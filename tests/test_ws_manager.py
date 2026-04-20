"""
Tests for WebSocket Connection Manager.

These test the core WebSocket management without a real server —
using mock WebSocket objects to verify connection lifecycle,
room management, and message broadcasting.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.ws_manager import ConnectionManager, SEND_TIMEOUT_SEC


@pytest.fixture
def manager():
    return ConnectionManager()


@pytest.fixture
def make_mock_ws():
    """Factory for creating mock WebSockets."""
    def _make():
        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        ws.client_state = MagicMock()
        # Simulate CONNECTED state
        from starlette.websockets import WebSocketState
        ws.client_state = WebSocketState.CONNECTED
        return ws
    return _make


class TestConnection:
    """Test connection/disconnection lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_registers_player(self, manager, make_mock_ws):
        ws = make_mock_ws()
        pid = "player-1"
        cid = "conn-1"

        info = await manager.connect(ws, pid, cid)

        assert info.player_id == pid
        assert info.connection_id == cid
        assert manager.active_connection_count == 1
        assert manager.is_player_connected(pid)
        ws.accept.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_removes_player(self, manager, make_mock_ws):
        ws = make_mock_ws()
        pid = "player-1"
        cid = "conn-1"

        await manager.connect(ws, pid, cid)
        info = await manager.disconnect(cid)

        assert info is not None
        assert info.player_id == pid
        assert manager.active_connection_count == 0
        assert not manager.is_player_connected(pid)

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self, manager, make_mock_ws):
        ws = make_mock_ws()
        await manager.connect(ws, "player-1", "conn-1")
        await manager.disconnect("conn-1")

        # Second disconnect should return None, not crash
        info = await manager.disconnect("conn-1")
        assert info is None

    @pytest.mark.asyncio
    async def test_duplicate_connection_evicts_old(self, manager, make_mock_ws):
        """A player opening a second connection should close the first."""
        ws1 = make_mock_ws()
        ws2 = make_mock_ws()
        pid = "player-1"

        await manager.connect(ws1, pid, "conn-1")
        assert manager.active_connection_count == 1

        await manager.connect(ws2, pid, "conn-2")
        assert manager.active_connection_count == 1  # Still 1, old was evicted

        # ws1 should have been forcibly closed
        ws1.close.assert_called_once()

        # The active connection should be ws2
        conn = manager.get_connection_by_player(pid)
        assert conn is not None
        assert conn.connection_id == "conn-2"

    @pytest.mark.asyncio
    async def test_multiple_players(self, manager, make_mock_ws):
        for i in range(5):
            ws = make_mock_ws()
            await manager.connect(ws, f"player-{i}", f"conn-{i}")

        assert manager.active_connection_count == 5
        assert len(manager.get_all_player_ids()) == 5


class TestRoomManagement:
    """Test room join/leave and membership tracking."""

    @pytest.mark.asyncio
    async def test_join_room(self, manager, make_mock_ws):
        ws = make_mock_ws()
        pid = "player-1"
        cid = "conn-1"
        room = "room-abc"

        await manager.connect(ws, pid, cid)
        manager.join_room(cid, room)

        members = manager.get_room_member_ids(room)
        assert pid in members

    @pytest.mark.asyncio
    async def test_leave_room(self, manager, make_mock_ws):
        ws = make_mock_ws()
        cid = "conn-1"

        await manager.connect(ws, "player-1", cid)
        manager.join_room(cid, "room-abc")
        manager.leave_room(cid)

        members = manager.get_room_member_ids("room-abc")
        assert len(members) == 0

    @pytest.mark.asyncio
    async def test_room_cleanup_on_disconnect(self, manager, make_mock_ws):
        ws = make_mock_ws()
        cid = "conn-1"

        await manager.connect(ws, "player-1", cid)
        manager.join_room(cid, "room-abc")

        await manager.disconnect(cid)

        members = manager.get_room_member_ids("room-abc")
        assert len(members) == 0

    @pytest.mark.asyncio
    async def test_switching_rooms(self, manager, make_mock_ws):
        """Joining a new room should automatically leave the old one."""
        ws = make_mock_ws()
        cid = "conn-1"
        pid = "player-1"

        await manager.connect(ws, pid, cid)
        manager.join_room(cid, "room-1")
        manager.join_room(cid, "room-2")

        assert pid not in manager.get_room_member_ids("room-1")
        assert pid in manager.get_room_member_ids("room-2")


class TestMessageSending:
    """Test personal and broadcast message delivery."""

    @pytest.mark.asyncio
    async def test_send_personal(self, manager, make_mock_ws):
        ws = make_mock_ws()
        pid = "player-1"

        await manager.connect(ws, pid, "conn-1")
        result = await manager.send_personal(pid, {"type": "test"})

        assert result is True
        ws.send_json.assert_called_once_with({"type": "test"})

    @pytest.mark.asyncio
    async def test_send_to_disconnected_player(self, manager):
        result = await manager.send_personal("nonexistent", {"type": "test"})
        assert result is False

    @pytest.mark.asyncio
    async def test_broadcast_to_room(self, manager, make_mock_ws):
        room = "room-abc"

        for i in range(3):
            ws = make_mock_ws()
            await manager.connect(ws, f"player-{i}", f"conn-{i}")
            manager.join_room(f"conn-{i}", room)

        sent = await manager.broadcast_to_room(room, {"type": "update"})
        assert sent == 3

    @pytest.mark.asyncio
    async def test_broadcast_with_exclude(self, manager, make_mock_ws):
        room = "room-abc"
        wss = []

        for i in range(3):
            ws = make_mock_ws()
            wss.append(ws)
            await manager.connect(ws, f"player-{i}", f"conn-{i}")
            manager.join_room(f"conn-{i}", room)

        sent = await manager.broadcast_to_room(
            room, {"type": "update"}, exclude_player_id="player-1"
        )
        assert sent == 2

    @pytest.mark.asyncio
    async def test_close_all(self, manager, make_mock_ws):
        for i in range(5):
            ws = make_mock_ws()
            await manager.connect(ws, f"player-{i}", f"conn-{i}")

        await manager.close_all()
        assert manager.active_connection_count == 0
