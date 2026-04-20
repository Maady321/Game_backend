"""
Game constants and enumerations.

Centralizing magic numbers and strings here makes them grep-able,
testable, and overridable. Never scatter literals across business logic.
"""

from __future__ import annotations

from enum import Enum


# ── WebSocket Message Types ──────────────────────────────────────────────
class WSMessageType(str, Enum):
    """All recognized WebSocket message types.

    Using str, Enum so these serialize directly to JSON strings without
    needing .value everywhere.
    """

    # Client → Server
    FIND_MATCH = "find_match"
    CANCEL_MATCH = "cancel_match"
    GAME_ACTION = "game_action"
    HEARTBEAT_PING = "heartbeat_ping"
    RECONNECT = "reconnect"

    # Server → Client
    MATCH_FOUND = "match_found"
    MATCH_CANCELLED = "match_cancelled"
    MATCHMAKING_QUEUED = "matchmaking_queued"
    GAME_STATE_UPDATE = "game_state_update"
    GAME_STARTED = "game_started"
    GAME_OVER = "game_over"
    HEARTBEAT_PONG = "heartbeat_pong"
    ERROR = "error"
    CONNECTED = "connected"
    PLAYER_DISCONNECTED = "player_disconnected"
    PLAYER_RECONNECTED = "player_reconnected"


# ── Game States ──────────────────────────────────────────────────────────
class GameStatus(str, Enum):
    WAITING = "waiting"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"  # When a player disconnects mid-game
    FINISHED = "finished"
    ABANDONED = "abandoned"


# ── Room States ──────────────────────────────────────────────────────────
class RoomStatus(str, Enum):
    OPEN = "open"
    FULL = "full"
    IN_GAME = "in_game"
    CLOSED = "closed"


# ── Player Connection States ────────────────────────────────────────────
class PlayerConnectionState(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    IN_MATCHMAKING = "in_matchmaking"
    IN_GAME = "in_game"


# ── Redis Key Prefixes ──────────────────────────────────────────────────
# Namespaced to avoid collisions in shared Redis instances.
class RedisKey:
    SESSION = "sess:{player_id}"
    ROOM = "room:{room_id}"
    ROOM_STATE = "room:{room_id}:state"
    ROOM_OWNER = "room:{room_id}:owner"
    MATCHMAKING_QUEUE = "mm:queue"
    MATCHMAKING_TIMESTAMPS = "mm:timestamps"
    PLAYER_ROOM = "player:{player_id}:room"
    INSTANCE_HEARTBEAT = "inst:{instance_id}:heartbeat"
    RATE_LIMIT = "rl:{player_id}:{window}"

    # Pub/Sub channel patterns
    CHANNEL_PLAYER = "ch:player:{player_id}"
    CHANNEL_ROOM = "ch:room:{room_id}"
    CHANNEL_MATCHMAKING = "ch:matchmaking"

    @classmethod
    def session(cls, player_id: str) -> str:
        return cls.SESSION.format(player_id=player_id)

    @classmethod
    def room(cls, room_id: str) -> str:
        return cls.ROOM.format(room_id=room_id)

    @classmethod
    def room_state(cls, room_id: str) -> str:
        return cls.ROOM_STATE.format(room_id=room_id)

    @classmethod
    def room_owner(cls, room_id: str) -> str:
        return cls.ROOM_OWNER.format(room_id=room_id)

    @classmethod
    def player_room(cls, player_id: str) -> str:
        return cls.PLAYER_ROOM.format(player_id=player_id)

    @classmethod
    def instance_heartbeat(cls, instance_id: str) -> str:
        return cls.INSTANCE_HEARTBEAT.format(instance_id=instance_id)

    @classmethod
    def rate_limit(cls, player_id: str, window: str) -> str:
        return cls.RATE_LIMIT.format(player_id=player_id, window=window)

    @classmethod
    def channel_player(cls, player_id: str) -> str:
        return cls.CHANNEL_PLAYER.format(player_id=player_id)

    @classmethod
    def channel_room(cls, room_id: str) -> str:
        return cls.CHANNEL_ROOM.format(room_id=room_id)


# ── Game-Specific Constants ─────────────────────────────────────────────
BOARD_SIZE = 10  # Example: 10x10 grid game
MAX_PLAYERS_PER_ROOM = 2
MIN_PLAYERS_TO_START = 2
MAX_TURNS = 100  # Safety cap to prevent infinite games
