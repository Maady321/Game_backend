"""
Pydantic schemas for WebSocket message serialization/deserialization.

Why Pydantic for WS messages?
- Type validation on every inbound message → rejects malformed input
  before it touches business logic (defense in depth).
- Discriminated unions via `type` field → single parser handles all
  message variants with exhaustive matching.
- orjson serialization for outbound messages → 3-6x faster than stdlib json.

All schemas use model_config with strict mode disabled for WebSocket
flexibility (clients may send ints as strings, etc.).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import orjson
from pydantic import BaseModel, Field, field_validator


def orjson_dumps(v: Any, *, default: Any = None) -> str:
    """Fast JSON serialization using orjson."""
    return orjson.dumps(v, default=default).decode()


# ─────────────────────────────────────────────────────────────────────────
# Base Message
# ─────────────────────────────────────────────────────────────────────────
class WSMessage(BaseModel):
    """Base WebSocket message. All messages must have a `type` field."""

    model_config = {"populate_by_name": True}

    type: str
    timestamp: datetime | None = None

    def to_json(self) -> str:
        return self.model_dump_json()


# ─────────────────────────────────────────────────────────────────────────
# Client → Server Messages
# ─────────────────────────────────────────────────────────────────────────
class FindMatchRequest(WSMessage):
    type: Literal["find_match"] = "find_match"


class CancelMatchRequest(WSMessage):
    type: Literal["cancel_match"] = "cancel_match"


class GameActionRequest(WSMessage):
    """Generic game action from client.

    The `action` field is game-specific (e.g., "move", "attack", "build").
    The `data` payload carries action parameters. Validated server-side
    by the game engine before application.
    """

    type: Literal["game_action"] = "game_action"
    action: str = Field(..., min_length=1, max_length=50)
    data: dict[str, Any] = Field(default_factory=dict)
    sequence_number: int = Field(..., ge=0)

    @field_validator("action")
    @classmethod
    def validate_action_name(cls, v: str) -> str:
        # Reject anything that isn't alphanumeric + underscores
        if not v.replace("_", "").isalnum():
            raise ValueError("Action name must be alphanumeric with underscores")
        return v


class HeartbeatPing(WSMessage):
    type: Literal["heartbeat_ping"] = "heartbeat_ping"


class ReconnectRequest(WSMessage):
    type: Literal["reconnect"] = "reconnect"
    session_token: str


# ─────────────────────────────────────────────────────────────────────────
# Server → Client Messages
# ─────────────────────────────────────────────────────────────────────────
class ConnectedResponse(WSMessage):
    type: Literal["connected"] = "connected"
    player_id: str
    session_id: str


class MatchmakingQueuedResponse(WSMessage):
    type: Literal["matchmaking_queued"] = "matchmaking_queued"
    estimated_wait_sec: int | None = None
    queue_position: int | None = None


class MatchFoundResponse(WSMessage):
    type: Literal["match_found"] = "match_found"
    room_id: str
    opponent_id: str
    opponent_username: str
    opponent_elo: int
    your_color: str  # e.g., "white" / "black" or "player_1" / "player_2"


class MatchCancelledResponse(WSMessage):
    type: Literal["match_cancelled"] = "match_cancelled"


class GameStartedResponse(WSMessage):
    type: Literal["game_started"] = "game_started"
    room_id: str
    initial_state: dict[str, Any]
    your_turn: bool


class GameStateUpdateResponse(WSMessage):
    type: Literal["game_state_update"] = "game_state_update"
    room_id: str
    state: dict[str, Any]
    last_action: dict[str, Any] | None = None
    your_turn: bool
    turn_number: int
    server_timestamp: float


class GameOverResponse(WSMessage):
    type: Literal["game_over"] = "game_over"
    room_id: str
    winner_id: str | None
    result: str  # "win", "loss", "draw"
    elo_delta: int
    final_state: dict[str, Any]


class HeartbeatPong(WSMessage):
    type: Literal["heartbeat_pong"] = "heartbeat_pong"
    server_time: float


class PlayerDisconnectedNotice(WSMessage):
    type: Literal["player_disconnected"] = "player_disconnected"
    player_id: str
    grace_period_sec: int


class PlayerReconnectedNotice(WSMessage):
    type: Literal["player_reconnected"] = "player_reconnected"
    player_id: str


class ErrorResponse(WSMessage):
    type: Literal["error"] = "error"
    code: str
    message: str
    details: dict[str, Any] | None = None


# ─────────────────────────────────────────────────────────────────────────
# REST API Schemas
# ─────────────────────────────────────────────────────────────────────────
class PlayerRegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_]+$")
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)


class PlayerLoginRequest(BaseModel):
    username: str
    password: str


class PlayerLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    player_id: str
    username: str
    elo_rating: int


class PlayerProfileResponse(BaseModel):
    player_id: str
    username: str
    elo_rating: int
    games_played: int
    games_won: int
    games_lost: int
    games_drawn: int
    win_rate: float
    created_at: datetime


class LeaderboardEntry(BaseModel):
    rank: int
    player_id: str
    username: str
    elo_rating: int
    games_played: int
    win_rate: float


class LeaderboardResponse(BaseModel):
    entries: list[LeaderboardEntry]
    total_players: int
    page: int
    page_size: int
