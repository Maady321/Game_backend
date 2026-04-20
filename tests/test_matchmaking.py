"""
Tests for the Matchmaking Service.

Uses fakeredis to test the full matchmaking flow including
the Lua script for atomic pair extraction.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.matchmaking import MatchmakingService, MATCHMAKING_LUA
from app.core.ws_manager import ConnectionManager
from app.utils.constants import RedisKey


@pytest.fixture
def pubsub_mock():
    mock = MagicMock()
    mock.publish_to_player = AsyncMock(return_value=1)
    return mock


@pytest.fixture
def matchmaking(redis_pool, connection_manager, pubsub_mock):
    return MatchmakingService(redis_pool, connection_manager, pubsub_mock)


class TestEnqueueDequeue:
    """Test queue management."""

    @pytest.mark.asyncio
    async def test_enqueue_player(self, matchmaking, redis_pool):
        await matchmaking.enqueue_player("player-1", 1200)

        # Verify in Redis sorted set
        score = await redis_pool.zscore(RedisKey.MATCHMAKING_QUEUE, "player-1")
        assert score == 1200.0

        # Verify timestamp recorded
        ts = await redis_pool.hget(RedisKey.MATCHMAKING_TIMESTAMPS, "player-1")
        assert ts is not None

    @pytest.mark.asyncio
    async def test_dequeue_player(self, matchmaking, redis_pool):
        await matchmaking.enqueue_player("player-1", 1200)
        removed = await matchmaking.dequeue_player("player-1")

        assert removed is True
        assert await redis_pool.zscore(RedisKey.MATCHMAKING_QUEUE, "player-1") is None

    @pytest.mark.asyncio
    async def test_dequeue_nonexistent(self, matchmaking):
        removed = await matchmaking.dequeue_player("ghost-player")
        assert removed is False

    @pytest.mark.asyncio
    async def test_queue_size(self, matchmaking):
        await matchmaking.enqueue_player("p1", 1000)
        await matchmaking.enqueue_player("p2", 1100)
        await matchmaking.enqueue_player("p3", 1200)

        size = await matchmaking.get_queue_size()
        assert size == 3


class TestMatchingLua:
    """Test the Lua script for atomic matching."""

    @pytest.mark.asyncio
    async def test_lua_matches_close_elo(self, matchmaking, redis_pool):
        """Two players with close ELO should be matched."""
        # Load Lua script
        lua_sha = await redis_pool.script_load(MATCHMAKING_LUA)

        # Add two players with close ELO
        await redis_pool.zadd(RedisKey.MATCHMAKING_QUEUE, {"p1": 1000, "p2": 1050})
        await redis_pool.hset(
            RedisKey.MATCHMAKING_TIMESTAMPS,
            mapping={"p1": str(time.time()), "p2": str(time.time())},
        )

        result = await redis_pool.evalsha(
            lua_sha, 2,
            RedisKey.MATCHMAKING_QUEUE,
            RedisKey.MATCHMAKING_TIMESTAMPS,
            100, 10, 500, time.time(),
        )

        assert result is not None
        assert len(result) == 4

        # Both should be removed from queue
        size = await redis_pool.zcard(RedisKey.MATCHMAKING_QUEUE)
        assert size == 0

    @pytest.mark.asyncio
    async def test_lua_no_match_far_elo(self, matchmaking, redis_pool):
        """Players with far ELO should NOT be matched (initially)."""
        lua_sha = await redis_pool.script_load(MATCHMAKING_LUA)

        await redis_pool.zadd(RedisKey.MATCHMAKING_QUEUE, {"p1": 1000, "p2": 2000})
        await redis_pool.hset(
            RedisKey.MATCHMAKING_TIMESTAMPS,
            mapping={"p1": str(time.time()), "p2": str(time.time())},
        )

        result = await redis_pool.evalsha(
            lua_sha, 2,
            RedisKey.MATCHMAKING_QUEUE,
            RedisKey.MATCHMAKING_TIMESTAMPS,
            100, 10, 500, time.time(),  # tolerance=100, max=500
        )

        assert result is None
        size = await redis_pool.zcard(RedisKey.MATCHMAKING_QUEUE)
        assert size == 2  # Both still in queue

    @pytest.mark.asyncio
    async def test_lua_tolerance_expansion(self, matchmaking, redis_pool):
        """After waiting, tolerance should expand to match far ELO."""
        lua_sha = await redis_pool.script_load(MATCHMAKING_LUA)

        join_time = time.time() - 40  # Waited 40 seconds

        await redis_pool.zadd(RedisKey.MATCHMAKING_QUEUE, {"p1": 1000, "p2": 1400})
        await redis_pool.hset(
            RedisKey.MATCHMAKING_TIMESTAMPS,
            mapping={"p1": str(join_time), "p2": str(join_time)},
        )

        # tolerance = 100 + 40*10 = 500 ≥ 400 ELO diff → should match
        result = await redis_pool.evalsha(
            lua_sha, 2,
            RedisKey.MATCHMAKING_QUEUE,
            RedisKey.MATCHMAKING_TIMESTAMPS,
            100, 10, 500, time.time(),
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_lua_single_player_no_match(self, matchmaking, redis_pool):
        """A single player in queue should not match."""
        lua_sha = await redis_pool.script_load(MATCHMAKING_LUA)

        await redis_pool.zadd(RedisKey.MATCHMAKING_QUEUE, {"p1": 1000})
        await redis_pool.hset(RedisKey.MATCHMAKING_TIMESTAMPS, "p1", str(time.time()))

        result = await redis_pool.evalsha(
            lua_sha, 2,
            RedisKey.MATCHMAKING_QUEUE,
            RedisKey.MATCHMAKING_TIMESTAMPS,
            100, 10, 500, time.time(),
        )

        assert result is None


class TestMatchCreation:
    """Test the full match creation flow."""

    @pytest.mark.asyncio
    @patch("app.core.matchmaking.get_settings")
    async def test_create_match_sends_notifications(
        self, mock_settings, matchmaking, redis_pool
    ):
        settings = MagicMock()
        settings.instance_id = "test-instance"
        settings.elo_tolerance_base = 100
        mock_settings.return_value = settings
        matchmaking._settings = settings

        room_id = await matchmaking._create_match("p1", 1000, "p2", 1050)

        assert room_id.startswith("room-")

        # Room should be registered in Redis
        room_data = await redis_pool.hgetall(f"room:{room_id}")
        assert room_data is not None
        assert len(room_data) > 0
