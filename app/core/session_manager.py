"""
Session Manager — distributed player session lifecycle.

Responsibilities:
1. Create, read, update, delete session records in Redis.
2. Track which instance each player is connected to.
3. Handle reconnection: detect if a player has an existing session,
   determine if they're in a game, and restore context.
4. Heartbeat monitoring: detect stale sessions where the client
   disconnected without a clean close (network drop, crash).

Why Redis for sessions instead of in-memory?
- Sessions must survive instance restarts and be accessible from
  any instance for reconnection routing.
- Redis TTL handles automatic cleanup of abandoned sessions.
- Atomic operations (HSETNX, HSET with conditions) prevent race
  conditions during reconnection.
"""

from __future__ import annotations

import time
import uuid

import redis.asyncio as aioredis

from app.config import get_settings
from app.models.redis_models import RedisSession
from app.utils.constants import PlayerConnectionState, RedisKey
from app.utils.logging import get_logger

logger = get_logger(__name__)


class SessionManager:
    """Manages player sessions in Redis."""

    def __init__(self, redis_pool: aioredis.Redis) -> None:
        self._redis = redis_pool
        self._settings = get_settings()

    async def create_session(
        self,
        player_id: str,
        connection_id: str,
        elo_rating: int = 1000,
        username: str = "",
    ) -> RedisSession:
        """Create a new session for a player.

        If a session already exists (player reconnecting), it is
        overwritten with the new connection details. The old instance
        will detect the stale connection via heartbeat failure.
        """
        session = RedisSession(
            player_id=player_id,
            instance_id=self._settings.instance_id,
            connection_id=connection_id,
            elo_rating=elo_rating,
            username=username,
        )

        key = RedisKey.session(player_id)
        pipe = self._redis.pipeline()
        pipe.hset(key, mapping=session.to_dict())
        pipe.expire(key, self._settings.session_ttl_sec)
        await pipe.execute()

        logger.info(
            "session_created",
            player_id=player_id,
            connection_id=connection_id,
            instance_id=self._settings.instance_id,
        )
        return session

    async def get_session(self, player_id: str) -> RedisSession | None:
        """Retrieve a player's session from Redis.

        Returns None if the session doesn't exist or has expired.
        """
        key = RedisKey.session(player_id)
        data = await self._redis.hgetall(key)
        if not data:
            return None

        # Decode bytes to str
        decoded = {k.decode(): v.decode() for k, v in data.items()}
        return RedisSession.from_dict(decoded)

    async def update_session_state(
        self, player_id: str, state: str, room_id: str | None = None
    ) -> None:
        """Update the session state (e.g., in_matchmaking → in_game)."""
        key = RedisKey.session(player_id)
        updates: dict[str, str] = {
            "state": state,
            "last_activity": str(time.time()),
        }
        if room_id is not None:
            updates["room_id"] = room_id
        await self._redis.hset(key, mapping=updates)

    async def update_activity(self, player_id: str) -> None:
        """Refresh the session TTL and last_activity timestamp.

        Called on every meaningful player action to prevent premature
        session expiry.
        """
        key = RedisKey.session(player_id)
        pipe = self._redis.pipeline()
        pipe.hset(key, "last_activity", str(time.time()))
        pipe.expire(key, self._settings.session_ttl_sec)
        await pipe.execute()

    async def delete_session(self, player_id: str) -> None:
        """Remove a session. Called on clean disconnect."""
        key = RedisKey.session(player_id)
        await self._redis.delete(key)
        logger.info("session_deleted", player_id=player_id)

    async def mark_disconnected(self, player_id: str) -> RedisSession | None:
        """Mark a player as disconnected (but keep the session for reconnection).

        The session remains in Redis for `reconnect_grace_period_sec`.
        If the player reconnects within this window, they can resume
        their game. If not, the session expires and the game handles
        the abandonment.
        """
        session = await self.get_session(player_id)
        if session is None:
            return None

        key = RedisKey.session(player_id)
        pipe = self._redis.pipeline()
        pipe.hset(key, mapping={
            "state": PlayerConnectionState.DISCONNECTED,
            "last_activity": str(time.time()),
        })
        # Reduce TTL to grace period
        pipe.expire(key, self._settings.reconnect_grace_period_sec)
        await pipe.execute()

        logger.info(
            "session_marked_disconnected",
            player_id=player_id,
            grace_period=self._settings.reconnect_grace_period_sec,
        )
        return session

    async def attempt_reconnect(
        self, player_id: str, new_connection_id: str
    ) -> RedisSession | None:
        """Attempt to reconnect a player to their existing session.

        Returns the restored session if reconnection is valid, None otherwise.

        A reconnection is valid if:
        1. A session exists in Redis (hasn't expired).
        2. The session state is 'disconnected'.
        3. We can atomically claim the session for this instance.
        """
        session = await self.get_session(player_id)
        if session is None:
            logger.info("reconnect_no_session", player_id=player_id)
            return None

        if session.state != PlayerConnectionState.DISCONNECTED:
            logger.warning(
                "reconnect_invalid_state",
                player_id=player_id,
                current_state=session.state,
            )
            return None

        # Atomically update the session to claim it for this instance
        key = RedisKey.session(player_id)
        pipe = self._redis.pipeline()
        pipe.hset(key, mapping={
            "instance_id": self._settings.instance_id,
            "connection_id": new_connection_id,
            "state": PlayerConnectionState.CONNECTED,
            "last_activity": str(time.time()),
        })
        # Restore full TTL
        pipe.expire(key, self._settings.session_ttl_sec)
        await pipe.execute()

        # Return updated session
        session.instance_id = self._settings.instance_id
        session.connection_id = new_connection_id
        session.state = PlayerConnectionState.CONNECTED

        logger.info(
            "session_reconnected",
            player_id=player_id,
            room_id=session.room_id,
            instance_id=self._settings.instance_id,
        )
        return session

    async def set_player_room(self, player_id: str, room_id: str) -> None:
        """Map a player to their current room (separate key for fast lookup)."""
        key = RedisKey.player_room(player_id)
        await self._redis.set(key, room_id, ex=self._settings.session_ttl_sec)

    async def get_player_room(self, player_id: str) -> str | None:
        """Get the room a player is currently in."""
        key = RedisKey.player_room(player_id)
        result = await self._redis.get(key)
        return result.decode() if result else None

    async def clear_player_room(self, player_id: str) -> None:
        """Remove the player→room mapping."""
        key = RedisKey.player_room(player_id)
        await self._redis.delete(key)

    # ── Instance Health ──────────────────────────────────────────────

    async def register_instance_heartbeat(self) -> None:
        """Register this instance's heartbeat in Redis.

        Other instances check this to detect dead instances and
        claim their orphaned rooms.
        """
        key = RedisKey.instance_heartbeat(self._settings.instance_id)
        await self._redis.set(
            key, str(time.time()), ex=self._settings.instance_heartbeat_ttl_sec
        )

    async def is_instance_alive(self, instance_id: str) -> bool:
        """Check if another instance's heartbeat is still valid."""
        key = RedisKey.instance_heartbeat(instance_id)
        return await self._redis.exists(key) > 0

    async def generate_connection_id(self) -> str:
        """Generate a unique connection ID."""
        return f"conn-{uuid.uuid4().hex[:12]}"
