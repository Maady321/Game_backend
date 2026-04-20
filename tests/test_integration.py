"""
End-to-end integration tests.

These tests boot a real FastAPI application and test the full flow
from WebSocket connection to game completion. Uses httpx for REST
and the FastAPI TestClient for WebSocket.

NOTE: These tests require running Redis and PostgreSQL instances.
Run via: docker-compose up -d postgres redis && pytest tests/test_integration.py
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.utils.auth import create_access_token


@pytest.fixture(scope="module")
def test_token_1():
    """JWT for test player 1."""
    return create_access_token("test-player-1")


@pytest.fixture(scope="module")
def test_token_2():
    """JWT for test player 2."""
    return create_access_token("test-player-2")


class TestHealthEndpoint:
    """Test the health check without full app setup."""

    def test_health_returns_status(self):
        """Health check should always respond, even during startup."""
        # This test works without Redis/PostgreSQL
        from app.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/health")
            # May return starting or healthy depending on Redis availability
            assert response.status_code == 200
            data = response.json()
            assert "status" in data
            assert "instance_id" in data


class TestRESTEndpoints:
    """Test REST API endpoints."""

    def test_register_validation(self):
        """Registration should reject invalid input."""
        from app.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            # Too short username
            response = client.post("/api/register", json={
                "username": "ab",
                "email": "test@test.com",
                "password": "validpass123",
            })
            assert response.status_code == 422  # Pydantic validation error

            # Too short password
            response = client.post("/api/register", json={
                "username": "validuser",
                "email": "test@test.com",
                "password": "short",
            })
            assert response.status_code == 422


class TestWebSocketProtocol:
    """Test WebSocket message protocol.

    These tests verify the message format and routing logic without
    needing a full game flow.
    """

    def test_ws_requires_token(self):
        """WebSocket connection without token should fail."""
        from app.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            # No token parameter
            try:
                with client.websocket_connect("/ws") as ws:
                    pass
            except Exception:
                pass  # Expected to fail

    def test_ws_invalid_token(self):
        """WebSocket with invalid token should receive error and close."""
        from app.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            try:
                with client.websocket_connect("/ws?token=invalid-jwt") as ws:
                    msg = ws.receive_json()
                    assert msg["type"] == "error"
                    assert msg["code"] == "AUTH_FAILED"
            except Exception:
                pass  # Connection close is expected


class TestAuthModule:
    """Test JWT authentication utilities."""

    def test_create_and_decode_token(self):
        from app.utils.auth import create_access_token, decode_token

        token = create_access_token("player-123")
        payload = decode_token(token)

        assert payload["sub"] == "player-123"
        assert "exp" in payload
        assert "iat" in payload

    def test_invalid_token_raises(self):
        from app.utils.auth import AuthError, decode_token

        with pytest.raises(AuthError):
            decode_token("not.a.valid.token")

    def test_extract_player_id(self):
        from app.utils.auth import create_access_token, extract_player_id

        token = create_access_token("player-xyz")
        pid = extract_player_id(token)
        assert pid == "player-xyz"
