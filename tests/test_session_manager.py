"""
Tests for Session Manager.

Tests Redis-backed session lifecycle: creation, recovery,
reconnection, and cleanup.
"""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest

from app.core.session_manager import SessionManager
from app.utils.constants import PlayerConnectionState, RedisKey


@pytest.fixture
def session_mgr(redis_pool):
    with patch("app.core.session_manager.get_settings") as mock:
        settings = MagicMock()
        settings.instance_id = "test-instance"
        settings.session_ttl_sec = 60
        settings.reconnect_grace_period_sec = 10
        settings.instance_heartbeat_ttl_sec = 15
        mock.return_value = settings

        mgr = SessionManager(redis_pool)
        mgr._settings = settings
        yield mgr


class TestSessionLifecycle:
    """Test session creation and deletion."""

    @pytest.mark.asyncio
    async def test_create_session(self, session_mgr, redis_pool):
        session = await session_mgr.create_session(
            player_id="player-1",
            connection_id="conn-1",
            elo_rating=1200,
            username="testuser",
        )

        assert session.player_id == "player-1"
        assert session.instance_id == "test-instance"
        assert session.elo_rating == 1200

        # Verify in Redis
        data = await redis_pool.hgetall(RedisKey.session("player-1"))
        assert data is not None
        assert len(data) > 0

    @pytest.mark.asyncio
    async def test_get_session(self, session_mgr):
        await session_mgr.create_session("player-1", "conn-1")
        session = await session_mgr.get_session("player-1")

        assert session is not None
        assert session.player_id == "player-1"

    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self, session_mgr):
        session = await session_mgr.get_session("ghost")
        assert session is None

    @pytest.mark.asyncio
    async def test_delete_session(self, session_mgr):
        await session_mgr.create_session("player-1", "conn-1")
        await session_mgr.delete_session("player-1")

        session = await session_mgr.get_session("player-1")
        assert session is None


class TestSessionState:
    """Test session state transitions."""

    @pytest.mark.asyncio
    async def test_update_state(self, session_mgr):
        await session_mgr.create_session("player-1", "conn-1")
        await session_mgr.update_session_state(
            "player-1", PlayerConnectionState.IN_MATCHMAKING
        )

        session = await session_mgr.get_session("player-1")
        assert session.state == "in_matchmaking"

    @pytest.mark.asyncio
    async def test_update_state_with_room(self, session_mgr):
        await session_mgr.create_session("player-1", "conn-1")
        await session_mgr.update_session_state(
            "player-1", PlayerConnectionState.IN_GAME, room_id="room-abc"
        )

        session = await session_mgr.get_session("player-1")
        assert session.state == "in_game"
        assert session.room_id == "room-abc"


class TestReconnection:
    """Test player reconnection flow."""

    @pytest.mark.asyncio
    async def test_mark_disconnected(self, session_mgr):
        await session_mgr.create_session("player-1", "conn-1")
        session = await session_mgr.mark_disconnected("player-1")

        assert session is not None
        updated = await session_mgr.get_session("player-1")
        assert updated.state == PlayerConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_successful_reconnect(self, session_mgr):
        await session_mgr.create_session(
            "player-1", "conn-1", elo_rating=1200
        )
        await session_mgr.update_session_state(
            "player-1", PlayerConnectionState.IN_GAME, room_id="room-abc"
        )
        await session_mgr.mark_disconnected("player-1")

        # Reconnect
        session = await session_mgr.attempt_reconnect("player-1", "conn-2")

        assert session is not None
        assert session.connection_id == "conn-2"
        assert session.state == PlayerConnectionState.CONNECTED
        assert session.room_id == "room-abc"  # Room preserved

    @pytest.mark.asyncio
    async def test_reconnect_no_session(self, session_mgr):
        session = await session_mgr.attempt_reconnect("ghost", "conn-1")
        assert session is None

    @pytest.mark.asyncio
    async def test_reconnect_not_disconnected(self, session_mgr):
        """Can't reconnect if session state isn't 'disconnected'."""
        await session_mgr.create_session("player-1", "conn-1")
        # State is "connected", not "disconnected"

        session = await session_mgr.attempt_reconnect("player-1", "conn-2")
        assert session is None


class TestPlayerRoom:
    """Test player→room mapping."""

    @pytest.mark.asyncio
    async def test_set_and_get_room(self, session_mgr):
        await session_mgr.set_player_room("player-1", "room-abc")
        room = await session_mgr.get_player_room("player-1")
        assert room == "room-abc"

    @pytest.mark.asyncio
    async def test_clear_room(self, session_mgr):
        await session_mgr.set_player_room("player-1", "room-abc")
        await session_mgr.clear_player_room("player-1")

        room = await session_mgr.get_player_room("player-1")
        assert room is None


class TestInstanceHealth:
    """Test instance heartbeat tracking."""

    @pytest.mark.asyncio
    async def test_register_heartbeat(self, session_mgr):
        await session_mgr.register_instance_heartbeat()

        alive = await session_mgr.is_instance_alive("test-instance")
        assert alive is True

    @pytest.mark.asyncio
    async def test_dead_instance(self, session_mgr):
        alive = await session_mgr.is_instance_alive("dead-instance")
        assert alive is False
