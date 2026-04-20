"""
Dependency Injection — singletons for shared infrastructure.

FastAPI's Depends() expects callables. These module-level singletons
are initialized in the lifespan handler and accessed globally.

Why global singletons instead of per-request creation?
- Redis connection pool, WebSocket manager, and game engine are
  long-lived resources. Creating them per-request would be wasteful
  and semantically wrong (you need the SAME connection manager to
  track all connections, not a fresh one per request).
- The lifespan handler guarantees proper startup/shutdown ordering.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings
from app.core.game_engine import GameEngine
from app.core.matchmaking import MatchmakingService
from app.core.redis_pubsub import RedisPubSubCoordinator
from app.core.session_manager import SessionManager
from app.core.ws_manager import ConnectionManager
from app.utils.logging import get_logger

logger = get_logger(__name__)

# ── Singleton Instances (initialized in lifespan) ────────────────────
_redis_pool: aioredis.Redis | None = None
_connection_manager: ConnectionManager | None = None
_session_manager: SessionManager | None = None
_matchmaking_service: MatchmakingService | None = None
_game_engine: GameEngine | None = None
_pubsub_coordinator: RedisPubSubCoordinator | None = None


async def init_dependencies() -> None:
    """Initialize all shared dependencies.

    Call order matters:
    1. Redis pool (everything depends on it)
    2. Connection manager (local, no deps)
    3. Pub/Sub coordinator (needs Redis)
    4. Session manager (needs Redis)
    5. Matchmaking service (needs Redis, conn manager, pub/sub)
    6. Game engine (needs Redis, conn manager, pub/sub)
    """
    global _redis_pool, _connection_manager, _session_manager
    global _matchmaking_service, _game_engine, _pubsub_coordinator

    settings = get_settings()

    # 1. Redis
    _redis_pool = aioredis.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=False,  # We handle decoding ourselves for performance
    )
    # Verify connection
    await _redis_pool.ping()
    logger.info("redis_connected", url=settings.redis_url)

    # 2. Connection Manager
    _connection_manager = ConnectionManager()

    # 3. Pub/Sub
    _pubsub_coordinator = RedisPubSubCoordinator(_redis_pool)

    # 4. Session Manager
    _session_manager = SessionManager(_redis_pool)

    # 5. Matchmaking
    _matchmaking_service = MatchmakingService(
        _redis_pool, _connection_manager, _pubsub_coordinator
    )

    # 6. Game Engine
    _game_engine = GameEngine(
        _redis_pool, _connection_manager, _pubsub_coordinator
    )

    # Start background services
    await _pubsub_coordinator.start(subscriptions=["ch:room:*", "ch:player:*"])
    await _matchmaking_service.start()
    await _game_engine.start()

    # Register Pub/Sub handler for cross-instance game events
    _pubsub_coordinator.register_pattern_handler("ch:player:*", _handle_player_message)
    _pubsub_coordinator.register_pattern_handler("ch:room:*", _handle_room_message)

    logger.info("all_dependencies_initialized")


async def shutdown_dependencies() -> None:
    """Gracefully shut down all dependencies in reverse order."""
    global _redis_pool

    if _game_engine:
        await _game_engine.stop()
    if _matchmaking_service:
        await _matchmaking_service.stop()
    if _pubsub_coordinator:
        await _pubsub_coordinator.stop()
    if _connection_manager:
        await _connection_manager.close_all()
    if _redis_pool:
        await _redis_pool.close()
        logger.info("redis_disconnected")

    logger.info("all_dependencies_shutdown")


# ── Pub/Sub Message Handlers ────────────────────────────────────────

async def _handle_player_message(channel: str, message: Any) -> None:
    """Handle messages published to player-specific channels.

    When Instance A publishes to ch:player:xyz, all instances subscribed
    to ch:player:* receive it. Only the instance that has player xyz
    connected locally should forward it.
    """
    from app.models.redis_models import PubSubMessage

    if _connection_manager is None:
        return

    # Extract player_id from channel: "ch:player:{player_id}"
    parts = channel.split(":")
    if len(parts) < 3:
        return
    player_id = parts[2]

    if not _connection_manager.is_player_connected(player_id):
        return  # Not our player

    # Forward the payload to the local WebSocket
    payload = message.payload if isinstance(message, PubSubMessage) else message
    await _connection_manager.send_personal(player_id, payload)


async def _handle_room_message(channel: str, message: Any) -> None:
    """Handle messages published to room channels."""
    from app.models.redis_models import PubSubMessage

    if _connection_manager is None:
        return

    parts = channel.split(":")
    if len(parts) < 3:
        return
    room_id = parts[2]

    payload = message.payload if isinstance(message, PubSubMessage) else message
    await _connection_manager.broadcast_to_room(room_id, payload)


# ── Accessors ────────────────────────────────────────────────────────

def get_redis() -> aioredis.Redis:
    if _redis_pool is None:
        raise RuntimeError("Redis not initialized")
    return _redis_pool


def get_connection_manager() -> ConnectionManager:
    if _connection_manager is None:
        raise RuntimeError("ConnectionManager not initialized")
    return _connection_manager


def get_session_manager() -> SessionManager:
    if _session_manager is None:
        raise RuntimeError("SessionManager not initialized")
    return _session_manager


def get_matchmaking_service() -> MatchmakingService:
    if _matchmaking_service is None:
        raise RuntimeError("MatchmakingService not initialized")
    return _matchmaking_service


def get_game_engine() -> GameEngine:
    if _game_engine is None:
        raise RuntimeError("GameEngine not initialized")
    return _game_engine


def get_pubsub_coordinator() -> RedisPubSubCoordinator:
    if _pubsub_coordinator is None:
        raise RuntimeError("PubSub not initialized")
    return _pubsub_coordinator
