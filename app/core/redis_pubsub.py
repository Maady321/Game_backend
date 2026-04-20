"""
Redis Pub/Sub Coordinator.

This module handles all cross-instance communication via Redis Pub/Sub.
It runs a background listener that subscribes to relevant channels and
dispatches incoming messages to registered handlers.

Architecture:
- One dedicated Redis connection for subscribing (Pub/Sub connections
  can't be used for regular commands — Redis protocol limitation).
- A separate connection pool for publishing (shared with other modules).
- Message handlers are registered per-channel-pattern using a callback
  registry. This decouples the Pub/Sub transport from business logic.

Why not use Redis Streams instead of Pub/Sub?
- Streams provide persistence and consumer groups, but we don't need
  message replay for real-time game state. If a message is missed
  (instance was down), the periodic state snapshot in Redis serves as
  the recovery mechanism. Pub/Sub is simpler and lower-latency for
  fire-and-forget broadcasting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.models.redis_models import PubSubMessage
from app.utils.constants import RedisKey
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Type alias for message handler callbacks
MessageHandler = Callable[[str, PubSubMessage], Coroutine[Any, Any, None]]


class RedisPubSubCoordinator:
    """Manages Redis Pub/Sub for cross-instance messaging.

    Lifecycle:
    1. start() — creates subscription connection, subscribes to channels,
       starts the listener loop as a background task.
    2. publish() — sends messages to channels (uses the shared pool).
    3. stop() — cancels the listener task and closes the subscription.
    """

    def __init__(self, redis_pool: aioredis.Redis) -> None:
        self._redis = redis_pool
        self._pubsub: aioredis.client.PubSub | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._pattern_handlers: dict[str, list[MessageHandler]] = {}
        self._running = False
        self._settings = get_settings()

    async def start(self, subscriptions: list[str] | None = None) -> None:
        """Start the Pub/Sub listener.

        Args:
            subscriptions: List of channel names or patterns to subscribe to.
                Patterns use '*' glob syntax (e.g., 'ch:room:*').
        """
        self._pubsub = self._redis.pubsub()
        self._running = True

        # Always subscribe to instance-specific channel
        instance_channel = f"ch:instance:{self._settings.instance_id}"
        await self._pubsub.subscribe(instance_channel)
        logger.info("pubsub_subscribed", channel=instance_channel)

        # Subscribe to additional channels/patterns
        if subscriptions:
            for sub in subscriptions:
                if "*" in sub:
                    await self._pubsub.psubscribe(sub)
                    logger.info("pubsub_psubscribed", pattern=sub)
                else:
                    await self._pubsub.subscribe(sub)
                    logger.info("pubsub_subscribed", channel=sub)

        # Start listener as background task
        self._listener_task = asyncio.create_task(
            self._listen_loop(), name="pubsub_listener"
        )
        logger.info("pubsub_listener_started")

    async def stop(self) -> None:
        """Stop the listener and clean up."""
        self._running = False

        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.punsubscribe()
            await self._pubsub.close()

        logger.info("pubsub_listener_stopped")

    def register_handler(self, channel: str, handler: MessageHandler) -> None:
        """Register a handler for messages on a specific channel."""
        if channel not in self._handlers:
            self._handlers[channel] = []
        self._handlers[channel].append(handler)

    def register_pattern_handler(self, pattern: str, handler: MessageHandler) -> None:
        """Register a handler for messages matching a channel pattern."""
        if pattern not in self._pattern_handlers:
            self._pattern_handlers[pattern] = []
        self._pattern_handlers[pattern].append(handler)

    async def subscribe_to_channel(self, channel: str) -> None:
        """Dynamically subscribe to a new channel (e.g., when a room is created)."""
        if self._pubsub:
            await self._pubsub.subscribe(channel)
            logger.debug("dynamic_subscribe", channel=channel)

    async def unsubscribe_from_channel(self, channel: str) -> None:
        """Unsubscribe from a channel (e.g., when a room is closed)."""
        if self._pubsub:
            await self._pubsub.unsubscribe(channel)
            logger.debug("dynamic_unsubscribe", channel=channel)

    async def publish(self, channel: str, message: PubSubMessage) -> int:
        """Publish a message to a channel.

        Returns the number of subscribers that received the message.
        """
        data = message.serialize()
        result = await self._redis.publish(channel, data)
        logger.debug(
            "pubsub_published",
            channel=channel,
            event_type=message.event,
            receivers=result,
        )
        return result

    async def publish_to_player(
        self, player_id: str, message: PubSubMessage
    ) -> int:
        """Publish a message to a specific player's channel."""
        channel = RedisKey.channel_player(player_id)
        return await self.publish(channel, message)

    async def publish_to_room(
        self, room_id: str, message: PubSubMessage
    ) -> int:
        """Publish a message to a room's channel."""
        channel = RedisKey.channel_room(room_id)
        return await self.publish(channel, message)

    async def _listen_loop(self) -> None:
        """Background loop that reads messages from subscribed channels.

        This loop runs for the lifetime of the application instance.
        It uses get_message() with a short timeout to remain responsive
        to cancellation.
        """
        logger.info("pubsub_listen_loop_started")

        while self._running:
            try:
                if self._pubsub is None:
                    break

                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )

                if message is None:
                    continue

                msg_type = message.get("type", "")
                if msg_type not in ("message", "pmessage"):
                    continue

                channel = message.get("channel", b"").decode()
                data = message.get("data", b"")

                if isinstance(data, bytes):
                    data = data.decode()

                try:
                    parsed = PubSubMessage.deserialize(data)
                except Exception as exc:
                    logger.warning("pubsub_parse_error", channel=channel, error=str(exc))
                    continue

                # Skip messages from ourselves
                if parsed.sender_instance_id == self._settings.instance_id:
                    continue

                # Dispatch to exact-match handlers
                await self._dispatch(channel, parsed)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("pubsub_listen_error", error=str(exc))
                await asyncio.sleep(1.0)  # Back off on unexpected errors

        logger.info("pubsub_listen_loop_exited")

    async def _dispatch(self, channel: str, message: PubSubMessage) -> None:
        """Dispatch a message to all registered handlers for the channel."""
        handlers = self._handlers.get(channel, [])

        # Also check pattern handlers
        for pattern, pattern_handlers in self._pattern_handlers.items():
            # Simple glob matching (convert pattern to check)
            prefix = pattern.split("*")[0]
            if channel.startswith(prefix):
                handlers.extend(pattern_handlers)

        if not handlers:
            logger.debug("pubsub_no_handler", channel=channel, event_type=message.event)
            return

        for handler in handlers:
            try:
                await handler(channel, message)
            except Exception as exc:
                logger.error(
                    "pubsub_handler_error",
                    channel=channel,
                    event=message.event,
                    error=str(exc),
                )
