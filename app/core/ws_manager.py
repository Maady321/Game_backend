"""
WebSocket Connection Manager.

This module manages all active WebSocket connections on THIS instance.
It is not distributed — each instance has its own ConnectionManager.
Cross-instance communication goes through Redis Pub/Sub.

Concurrency model:
- FastAPI runs one asyncio event loop per process.
- Each WebSocket connection is a long-lived coroutine on that loop.
- Connection/disconnection mutate shared state (the _connections dict).
- Since asyncio is single-threaded cooperative multitasking, dict
  mutations are atomic at the await-boundary level — no locks needed
  for simple dict operations.
- However, broadcast operations (iterating + sending) DO need care:
  we snapshot the recipient list before iterating to avoid
  RuntimeError from dict mutation during iteration.

Backpressure:
- If a client's send buffer is full (slow consumer), we don't want to
  block the game loop. We use asyncio.wait_for with a timeout on each
  send, and disconnect slow consumers rather than letting them degrade
  the server.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.utils.constants import WSMessageType
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Timeout for individual message sends. If a client can't accept a
# message within this window, they're too slow and get disconnected.
SEND_TIMEOUT_SEC = 5.0


@dataclass
class ConnectionInfo:
    """Metadata about a single WebSocket connection."""

    websocket: WebSocket
    player_id: str
    connection_id: str
    room_id: str | None = None
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    message_count: int = 0


class ConnectionManager:
    """Manages WebSocket connections for a single server instance.

    Thread-safety: This class is designed for single-threaded asyncio.
    All public methods are coroutines. Never call them from threads.
    """

    def __init__(self) -> None:
        # Primary index: connection_id → ConnectionInfo
        self._connections: dict[str, ConnectionInfo] = {}
        # Secondary index: player_id → connection_id (for fast lookup)
        self._player_connections: dict[str, str] = {}
        # Room membership: room_id → set of connection_ids
        self._room_members: dict[str, set[str]] = {}

    @property
    def active_connection_count(self) -> int:
        return len(self._connections)

    # ── Connection Lifecycle ─────────────────────────────────────────

    async def connect(
        self,
        websocket: WebSocket,
        player_id: str,
        connection_id: str,
    ) -> ConnectionInfo:
        """Accept a WebSocket connection and register it.

        If the player already has an active connection (e.g., opened a
        second tab), the old connection is forcibly closed. This prevents
        "ghost" connections and ensures one-player-one-connection invariant.
        """
        # Evict existing connection for this player (if any)
        existing_conn_id = self._player_connections.get(player_id)
        if existing_conn_id and existing_conn_id in self._connections:
            logger.warning(
                "evicting_duplicate_connection",
                player_id=player_id,
                old_conn_id=existing_conn_id,
                new_conn_id=connection_id,
            )
            await self._force_close(existing_conn_id, reason="duplicate_connection")

        await websocket.accept()

        info = ConnectionInfo(
            websocket=websocket,
            player_id=player_id,
            connection_id=connection_id,
        )
        self._connections[connection_id] = info
        self._player_connections[player_id] = connection_id

        logger.info(
            "ws_connected",
            player_id=player_id,
            connection_id=connection_id,
            total_connections=self.active_connection_count,
        )
        return info

    async def disconnect(self, connection_id: str) -> ConnectionInfo | None:
        """Remove a connection from all registries.

        Returns the ConnectionInfo if found, None if already removed
        (idempotent — safe to call multiple times).
        """
        info = self._connections.pop(connection_id, None)
        if info is None:
            return None

        # Clean up secondary indexes
        if self._player_connections.get(info.player_id) == connection_id:
            del self._player_connections[info.player_id]

        # Remove from room membership
        if info.room_id and info.room_id in self._room_members:
            self._room_members[info.room_id].discard(connection_id)
            if not self._room_members[info.room_id]:
                del self._room_members[info.room_id]

        logger.info(
            "ws_disconnected",
            player_id=info.player_id,
            connection_id=connection_id,
            total_connections=self.active_connection_count,
        )
        return info

    async def _force_close(self, connection_id: str, reason: str = "server_close") -> None:
        """Forcibly close a WebSocket connection."""
        info = self._connections.get(connection_id)
        if info and info.websocket.client_state == WebSocketState.CONNECTED:
            try:
                await info.websocket.close(code=1008, reason=reason)
            except Exception:
                pass  # Best effort — connection might already be dead
        await self.disconnect(connection_id)

    # ── Room Management ──────────────────────────────────────────────

    def join_room(self, connection_id: str, room_id: str) -> None:
        """Add a connection to a room's member set."""
        info = self._connections.get(connection_id)
        if info is None:
            return

        # Leave current room first (if any)
        if info.room_id and info.room_id != room_id:
            self.leave_room(connection_id)

        info.room_id = room_id
        if room_id not in self._room_members:
            self._room_members[room_id] = set()
        self._room_members[room_id].add(connection_id)

        logger.debug("joined_room", player_id=info.player_id, room_id=room_id)

    def leave_room(self, connection_id: str) -> None:
        """Remove a connection from its current room."""
        info = self._connections.get(connection_id)
        if info is None or info.room_id is None:
            return

        room_id = info.room_id
        info.room_id = None

        if room_id in self._room_members:
            self._room_members[room_id].discard(connection_id)
            if not self._room_members[room_id]:
                del self._room_members[room_id]

    def get_room_member_ids(self, room_id: str) -> list[str]:
        """Get player_ids of all connections in a room on THIS instance."""
        conn_ids = self._room_members.get(room_id, set())
        result = []
        for cid in conn_ids:
            info = self._connections.get(cid)
            if info:
                result.append(info.player_id)
        return result

    # ── Message Sending ──────────────────────────────────────────────

    async def send_personal(self, player_id: str, message: dict[str, Any]) -> bool:
        """Send a message to a specific player (if connected to this instance).

        Returns True if the message was sent, False if the player is not
        connected here.
        """
        conn_id = self._player_connections.get(player_id)
        if conn_id is None:
            return False
        return await self._send_to_connection(conn_id, message)

    async def broadcast_to_room(
        self,
        room_id: str,
        message: dict[str, Any],
        exclude_player_id: str | None = None,
    ) -> int:
        """Broadcast a message to all room members on THIS instance.

        Args:
            room_id: Target room.
            message: JSON-serializable message dict.
            exclude_player_id: Optional player to skip (e.g., the sender).

        Returns:
            Number of players the message was successfully sent to.
        """
        conn_ids = self._room_members.get(room_id, set()).copy()  # Snapshot
        sent_count = 0

        tasks = []
        for conn_id in conn_ids:
            info = self._connections.get(conn_id)
            if info and info.player_id != exclude_player_id:
                tasks.append(self._send_to_connection(conn_id, message))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            sent_count = sum(1 for r in results if r is True)

        return sent_count

    async def _send_to_connection(
        self, connection_id: str, message: dict[str, Any]
    ) -> bool:
        """Send a message to a specific connection with timeout.

        If the send times out, the connection is considered a slow consumer
        and is forcibly closed. This prevents one stuck client from
        blocking the entire broadcast.
        """
        info = self._connections.get(connection_id)
        if info is None:
            return False

        try:
            await asyncio.wait_for(
                info.websocket.send_json(message),
                timeout=SEND_TIMEOUT_SEC,
            )
            info.last_activity = time.time()
            info.message_count += 1
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "slow_consumer_disconnected",
                player_id=info.player_id,
                connection_id=connection_id,
            )
            await self._force_close(connection_id, reason="slow_consumer")
            return False
        except (WebSocketDisconnect, RuntimeError, Exception) as exc:
            logger.debug(
                "send_failed",
                player_id=info.player_id,
                connection_id=connection_id,
                error=str(exc),
            )
            await self.disconnect(connection_id)
            return False

    # ── Lookup Helpers ───────────────────────────────────────────────

    def get_connection_by_player(self, player_id: str) -> ConnectionInfo | None:
        """Look up connection info by player_id."""
        conn_id = self._player_connections.get(player_id)
        if conn_id:
            return self._connections.get(conn_id)
        return None

    def get_connection(self, connection_id: str) -> ConnectionInfo | None:
        """Look up connection info by connection_id."""
        return self._connections.get(connection_id)

    def is_player_connected(self, player_id: str) -> bool:
        return player_id in self._player_connections

    def get_all_player_ids(self) -> list[str]:
        return list(self._player_connections.keys())

    # ── Maintenance ──────────────────────────────────────────────────

    async def close_all(self) -> None:
        """Close all connections. Called during graceful shutdown."""
        conn_ids = list(self._connections.keys())
        for conn_id in conn_ids:
            await self._force_close(conn_id, reason="server_shutdown")
        logger.info("all_connections_closed", count=len(conn_ids))
