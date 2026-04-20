"""
WebSocket endpoint — the main entry point for real-time game communication.

Connection flow:
1. Client connects to /ws?token=<JWT>
2. Server authenticates via JWT, creates session in Redis.
3. Server sends "connected" message with session details.
4. Client sends messages (find_match, game_action, heartbeat_ping).
5. Server routes messages to appropriate service handlers.
6. On disconnect, session is marked for grace period reconnection.

Message routing:
- Each inbound message has a `type` field that determines which
  handler processes it. This is essentially a command pattern where
  the WebSocket acts as a transport and the type field is the command.

Why a single WS endpoint instead of multiple?
- WebSocket connections are expensive (TCP handshake + upgrade).
  Multiplexing all game communication over one connection reduces
  overhead and simplifies the connection lifecycle.
- The `type` discriminator provides clean separation without extra
  connections.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from app.api.middleware import check_ws_rate_limit
from app.config import get_settings
from app.dependencies import (
    get_connection_manager,
    get_game_engine,
    get_matchmaking_service,
    get_redis,
    get_session_manager,
)
from app.models.schemas import (
    ConnectedResponse,
    ErrorResponse,
    HeartbeatPong,
)
from app.utils.auth import AuthError, extract_player_id
from app.utils.constants import PlayerConnectionState, WSMessageType
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(..., description="JWT authentication token"),
) -> None:
    """Main WebSocket endpoint for all real-time game communication."""

    settings = get_settings()
    conn_manager = get_connection_manager()
    session_mgr = get_session_manager()
    redis_pool = get_redis()

    # ── 1. Authenticate ──────────────────────────────────────────────
    try:
        player_id = extract_player_id(token)
    except AuthError as exc:
        await websocket.accept()
        await websocket.send_json(
            ErrorResponse(code="AUTH_FAILED", message=exc.detail).model_dump()
        )
        await websocket.close(code=1008, reason="Authentication failed")
        return

    # ── 2. Register Connection ───────────────────────────────────────
    connection_id = f"conn-{uuid.uuid4().hex[:12]}"
    conn_info = await conn_manager.connect(websocket, player_id, connection_id)

    # ── 3. Create/Restore Session ────────────────────────────────────
    existing_session = await session_mgr.get_session(player_id)

    if existing_session and existing_session.state == PlayerConnectionState.DISCONNECTED:
        # Reconnection — restore session
        session = await session_mgr.attempt_reconnect(player_id, connection_id)
        if session and session.room_id:
            # Player was in a game — restore game context
            game_engine = get_game_engine()
            await game_engine.handle_player_reconnect(session.room_id, player_id)
            logger.info("player_reconnected_to_game", player_id=player_id, room_id=session.room_id)
    else:
        # Fresh connection
        session = await session_mgr.create_session(
            player_id=player_id,
            connection_id=connection_id,
        )

    # ── 4. Send Connected Acknowledgment ─────────────────────────────
    await conn_manager.send_personal(
        player_id,
        ConnectedResponse(
            player_id=player_id,
            session_id=connection_id,
        ).model_dump(),
    )

    # ── 5. Message Loop ──────────────────────────────────────────────
    try:
        while True:
            raw = await websocket.receive_text()

            # Rate limiting
            allowed = await check_ws_rate_limit(
                redis_pool, player_id, settings.rate_limit_ws_messages_per_sec
            )
            if not allowed:
                await conn_manager.send_personal(
                    player_id,
                    ErrorResponse(
                        code="RATE_LIMITED",
                        message="Too many messages. Slow down.",
                    ).model_dump(),
                )
                continue

            # Parse message
            try:
                message = orjson.loads(raw)
            except (orjson.JSONDecodeError, ValueError):
                await conn_manager.send_personal(
                    player_id,
                    ErrorResponse(code="INVALID_JSON", message="Invalid JSON").model_dump(),
                )
                continue

            msg_type = message.get("type")
            if not msg_type:
                await conn_manager.send_personal(
                    player_id,
                    ErrorResponse(code="MISSING_TYPE", message="Message missing 'type' field").model_dump(),
                )
                continue

            # Update session activity
            await session_mgr.update_activity(player_id)

            # Route to handler
            await _route_message(player_id, msg_type, message)

    except WebSocketDisconnect:
        logger.info("ws_clean_disconnect", player_id=player_id)
    except Exception as exc:
        logger.error("ws_error", player_id=player_id, error=str(exc))
    finally:
        # ── 6. Cleanup on Disconnect ─────────────────────────────────
        await _handle_disconnect(player_id, connection_id)


async def _route_message(
    player_id: str, msg_type: str, message: dict[str, Any]
) -> None:
    """Route an inbound message to the appropriate handler."""

    conn_manager = get_connection_manager()

    if msg_type == WSMessageType.FIND_MATCH:
        matchmaking = get_matchmaking_service()
        session_mgr = get_session_manager()

        # Get player's ELO from session
        session = await session_mgr.get_session(player_id)
        elo = session.elo_rating if session else 1000

        await session_mgr.update_session_state(player_id, PlayerConnectionState.IN_MATCHMAKING)
        await matchmaking.enqueue_player(player_id, elo)

    elif msg_type == WSMessageType.CANCEL_MATCH:
        matchmaking = get_matchmaking_service()
        session_mgr = get_session_manager()
        await matchmaking.dequeue_player(player_id)
        await session_mgr.update_session_state(player_id, PlayerConnectionState.CONNECTED)

    elif msg_type == WSMessageType.GAME_ACTION:
        game_engine = get_game_engine()
        session_mgr = get_session_manager()

        action = message.get("action", "")
        data = message.get("data", {})
        seq = message.get("sequence_number", 0)

        # Find the player's room
        room_id = await session_mgr.get_player_room(player_id)
        if not room_id:
            await conn_manager.send_personal(
                player_id,
                ErrorResponse(code="NOT_IN_GAME", message="You are not in a game").model_dump(),
            )
            return

        await game_engine.process_action(room_id, player_id, action, data, seq)

    elif msg_type == WSMessageType.HEARTBEAT_PING:
        await conn_manager.send_personal(
            player_id,
            HeartbeatPong(server_time=time.time()).model_dump(),
        )

    else:
        await conn_manager.send_personal(
            player_id,
            ErrorResponse(
                code="UNKNOWN_TYPE",
                message=f"Unknown message type: {msg_type}",
            ).model_dump(),
        )


async def _handle_disconnect(player_id: str, connection_id: str) -> None:
    """Clean up after a player disconnects."""
    conn_manager = get_connection_manager()
    session_mgr = get_session_manager()
    game_engine = get_game_engine()
    matchmaking = get_matchmaking_service()

    # Remove from connection manager
    conn_info = await conn_manager.disconnect(connection_id)

    # Remove from matchmaking queue (if they were searching)
    await matchmaking.dequeue_player(player_id)

    # Mark session as disconnected (starts grace period)
    session = await session_mgr.mark_disconnected(player_id)

    # Notify game engine (if player was in a game)
    if session and session.room_id:
        await game_engine.handle_player_disconnect(session.room_id, player_id)
