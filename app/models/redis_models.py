"""
Redis data structure definitions and serialization helpers.

This module defines the shapes of data stored in Redis and provides
helper functions for consistent serialization. It acts as a contract
between all modules that read/write Redis — preventing ad-hoc key
construction or inconsistent field names.

Why explicit models instead of raw dicts?
- Type hints catch field name typos at dev time
- Centralized TTL management
- Serialization logic in one place (not scattered across services)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import orjson


def _now() -> float:
    return time.time()


@dataclass
class RedisSession:
    """Player session stored in Redis Hash.

    Key: sess:{player_id}
    TTL: config.session_ttl_sec (refreshed on activity)

    This is the single source of truth for "where is this player right now?"
    Any instance can look up a player's session to route messages or
    handle reconnection.
    """

    player_id: str
    instance_id: str
    connection_id: str
    room_id: str | None = None
    state: str = "connected"  # connected | disconnected | in_matchmaking | in_game
    elo_rating: int = 1000
    username: str = ""
    connected_at: float = field(default_factory=_now)
    last_activity: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, str]:
        """Serialize to flat string dict for Redis HSET."""
        return {
            "player_id": self.player_id,
            "instance_id": self.instance_id,
            "connection_id": self.connection_id,
            "room_id": self.room_id or "",
            "state": self.state,
            "elo_rating": str(self.elo_rating),
            "username": self.username,
            "connected_at": str(self.connected_at),
            "last_activity": str(self.last_activity),
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> RedisSession:
        """Deserialize from Redis HGETALL result."""
        return cls(
            player_id=data["player_id"],
            instance_id=data["instance_id"],
            connection_id=data["connection_id"],
            room_id=data.get("room_id") or None,
            state=data.get("state", "connected"),
            elo_rating=int(data.get("elo_rating", 1000)),
            username=data.get("username", ""),
            connected_at=float(data.get("connected_at", 0)),
            last_activity=float(data.get("last_activity", 0)),
        )


@dataclass
class RedisRoom:
    """Room metadata stored in Redis Hash.

    Key: room:{room_id}
    No TTL — cleaned up explicitly when the game ends.

    The actual game state is in a separate key (room:{room_id}:state)
    to allow independent read/write patterns and avoid huge HGETALL
    calls when you only need metadata.
    """

    room_id: str
    owner_instance_id: str
    player_ids: list[str] = field(default_factory=list)
    status: str = "open"  # open | full | in_game | closed
    created_at: float = field(default_factory=_now)
    match_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            "room_id": self.room_id,
            "owner_instance_id": self.owner_instance_id,
            "player_ids": orjson.dumps(self.player_ids).decode(),
            "status": self.status,
            "created_at": str(self.created_at),
            "match_id": self.match_id or "",
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> RedisRoom:
        return cls(
            room_id=data["room_id"],
            owner_instance_id=data["owner_instance_id"],
            player_ids=orjson.loads(data.get("player_ids", "[]")),
            status=data.get("status", "open"),
            created_at=float(data.get("created_at", 0)),
            match_id=data.get("match_id") or None,
        )


@dataclass
class RedisGameState:
    """Serializable game state snapshot for Redis.

    Key: room:{room_id}:state
    Written periodically (every 1-5 seconds) by the owning instance
    as a recovery checkpoint. NOT updated on every tick — that would
    be too expensive.

    The `board` and `extra` fields are game-specific. This is a generic
    container that any game implementation can use.
    """

    room_id: str
    turn_number: int = 0
    current_player_id: str = ""
    board: list[list[int]] = field(default_factory=lambda: [[0] * 10 for _ in range(10)])
    scores: dict[str, int] = field(default_factory=dict)
    action_history: list[dict[str, Any]] = field(default_factory=list)
    status: str = "in_progress"
    winner_id: str | None = None
    snapshot_time: float = field(default_factory=_now)
    extra: dict[str, Any] = field(default_factory=dict)

    def serialize(self) -> str:
        """Serialize to JSON string for Redis SET."""
        return orjson.dumps(self.__dict__).decode()

    @classmethod
    def deserialize(cls, data: str) -> RedisGameState:
        """Deserialize from Redis GET result."""
        parsed = orjson.loads(data)
        return cls(**parsed)

    def to_client_dict(self, for_player_id: str) -> dict[str, Any]:
        """Create a client-safe view of the state.

        Filter out information the client shouldn't see (e.g., opponent's
        hidden cards in a card game). Override this for game-specific
        information hiding.
        """
        return {
            "turn_number": self.turn_number,
            "current_player_id": self.current_player_id,
            "board": self.board,
            "scores": self.scores,
            "status": self.status,
            "winner_id": self.winner_id,
        }


@dataclass
class PubSubMessage:
    """Standard envelope for Redis Pub/Sub messages.

    Using a consistent envelope means every subscriber can parse the
    outer structure without knowing the inner payload type upfront.
    """

    event: str  # "game_action", "state_update", "player_joined", etc.
    room_id: str
    sender_instance_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=_now)

    def serialize(self) -> str:
        return orjson.dumps(self.__dict__).decode()

    @classmethod
    def deserialize(cls, data: str | bytes) -> PubSubMessage:
        parsed = orjson.loads(data)
        return cls(**parsed)
