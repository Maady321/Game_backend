"""
Test fixtures and shared configuration.

Uses fakeredis for Redis mocking — it runs an in-process Redis-compatible
server with Lua script support, so matchmaking Lua scripts work in tests
without a real Redis instance.

Uses an in-memory SQLite database for PostgreSQL mocking in unit tests.
Integration tests use a real PostgreSQL via docker-compose.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import fakeredis.aioredis

from app.config import Settings, get_settings
from app.core.ws_manager import ConnectionManager


@pytest.fixture(scope="session")
def event_loop():
    """Use a single event loop for all async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def redis_pool():
    """Fake Redis pool for testing."""
    pool = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield pool
    await pool.flushall()
    await pool.close()


@pytest.fixture(autouse=True)
def patch_redis(redis_pool):
    """Patch aioredis.from_url to use fake redis globally in all tests."""
    from unittest.mock import patch
    with patch("redis.asyncio.from_url", return_value=redis_pool):
        yield


@pytest_asyncio.fixture
async def connection_manager():
    """Fresh ConnectionManager for each test."""
    mgr = ConnectionManager()
    yield mgr
    await mgr.close_all()


@pytest.fixture
def mock_websocket():
    """Create a mock WebSocket for testing."""
    ws = AsyncMock()
    ws.client_state = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.send_text = AsyncMock()
    ws.receive_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.fixture
def player_id():
    """Generate a unique player ID for testing."""
    return str(uuid.uuid4())


@pytest.fixture
def room_id():
    """Generate a unique room ID for testing."""
    return f"room-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def connection_id():
    """Generate a unique connection ID for testing."""
    return f"conn-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def settings():
    """Test settings with short timeouts for faster tests."""
    return Settings(
        app_env="test",
        app_debug=True,
        redis_url="redis://localhost:6379/15",  # Use DB 15 for tests
        session_ttl_sec=60,
        reconnect_grace_period_sec=5,
        heartbeat_interval_sec=2,
        heartbeat_timeout_sec=5,
        matchmaking_interval_ms=100,
    )
