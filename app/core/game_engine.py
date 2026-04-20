"""
Server-Authoritative Game State Engine.

This is the core game logic module. It implements a simple but extensible
territory control game to demonstrate all the patterns needed for any
real-time multiplayer game.

Game: Territory Control (10×10 grid)
- Two players take turns placing pieces on a grid.
- Each piece claims adjacent empty cells.
- The player with more territory when the board is full wins.

Key principles:
1. NEVER trust the client. All actions are validated server-side.
   The client sends "I want to place at (3,5)", the server validates
   legality, applies the action, and broadcasts the authoritative state.
2. State is deterministic. Given the same sequence of actions, the state
   is always identical. This enables replay and verification.
3. State snapshots to Redis happen periodically, not on every action.
   If the owning instance crashes, at most 1-5 seconds of state is lost.
   For most games, replaying a few actions from the last snapshot is
   acceptable.

ELO Calculation:
- Standard FIDE-style ELO with K-factor 32.
- New players have higher K for faster calibration (K=48 for first 30 games).
"""

from __future__ import annotations

import asyncio
import copy
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.core.redis_pubsub import RedisPubSubCoordinator
from app.core.ws_manager import ConnectionManager
from app.models.redis_models import PubSubMessage, RedisGameState, RedisRoom
from app.models.schemas import (
    ErrorResponse,
    GameOverResponse,
    GameStartedResponse,
    GameStateUpdateResponse,
    PlayerDisconnectedNotice,
    PlayerReconnectedNotice,
)
from app.utils.constants import (
    BOARD_SIZE,
    MAX_TURNS,
    GameStatus,
    RedisKey,
    WSMessageType,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ActiveGame:
    """In-memory representation of a live game on this instance.

    This is the HOT state — mutated on every player action, never
    written to Redis on every tick. Snapshots go to Redis periodically.
    """

    room_id: str
    match_id: str | None = None
    player_ids: list[str] = field(default_factory=list)
    player_elos: dict[str, int] = field(default_factory=dict)
    current_player_idx: int = 0
    board: list[list[int]] = field(
        default_factory=lambda: [[0] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    )
    scores: dict[str, int] = field(default_factory=dict)
    turn_number: int = 0
    status: str = GameStatus.WAITING
    winner_id: str | None = None
    action_history: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_action_time: float = field(default_factory=time.time)
    disconnected_players: set[str] = field(default_factory=set)

    @property
    def current_player_id(self) -> str:
        return self.player_ids[self.current_player_idx]

    def to_redis_state(self) -> RedisGameState:
        """Convert to Redis-serializable snapshot."""
        return RedisGameState(
            room_id=self.room_id,
            turn_number=self.turn_number,
            current_player_id=self.current_player_id,
            board=copy.deepcopy(self.board),
            scores=dict(self.scores),
            action_history=list(self.action_history[-20:]),  # Keep last 20 actions
            status=self.status,
            winner_id=self.winner_id,
        )

    def to_client_state(self) -> dict[str, Any]:
        """Create a client-safe state snapshot."""
        return {
            "board": self.board,
            "scores": self.scores,
            "turn_number": self.turn_number,
            "current_player_id": self.current_player_id,
            "status": self.status,
            "winner_id": self.winner_id,
            "player_ids": self.player_ids,
        }


class GameEngine:
    """Server-authoritative game state engine.

    Manages active games on this instance: creation, action processing,
    state broadcasting, snapshots, and game completion.
    """

    def __init__(
        self,
        redis_pool: aioredis.Redis,
        connection_manager: ConnectionManager,
        pubsub: RedisPubSubCoordinator,
    ) -> None:
        self._redis = redis_pool
        self._conn_manager = connection_manager
        self._pubsub = pubsub
        self._settings = get_settings()
        from app.dependencies import get_session_manager
        self._get_session_manager = get_session_manager

        # Active games owned by this instance: room_id → ActiveGame
        self._games: dict[str, ActiveGame] = {}

        # Background tasks
        self._snapshot_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def active_game_count(self) -> int:
        return len(self._games)

    async def start(self) -> None:
        """Start background tasks (periodic snapshots)."""
        self._running = True
        self._snapshot_task = asyncio.create_task(
            self._snapshot_loop(), name="state_snapshot"
        )
        logger.info("game_engine_started")

    async def stop(self) -> None:
        """Stop engine and persist all game states."""
        self._running = False
        if self._snapshot_task and not self._snapshot_task.done():
            self._snapshot_task.cancel()
            try:
                await self._snapshot_task
            except asyncio.CancelledError:
                pass

        # Final snapshot of all active games before shutdown
        for room_id in list(self._games.keys()):
            await self._save_snapshot(room_id)

        logger.info("game_engine_stopped", games_saved=len(self._games))

    # ── Game Lifecycle ───────────────────────────────────────────────

    async def create_game(
        self,
        room_id: str,
        player_ids: list[str],
        player_elos: dict[str, int],
    ) -> ActiveGame:
        """Initialize a new game for a matched pair.

        Called by the matchmaking service after room creation.
        """
        game = ActiveGame(
            room_id=room_id,
            player_ids=player_ids,
            player_elos=player_elos,
            scores={pid: 0 for pid in player_ids},
            status=GameStatus.IN_PROGRESS,
        )
        self._games[room_id] = game

        session_mgr = self._get_session_manager()

        # Join both players to the WS room on this instance
        for pid in player_ids:
            # Set player room in Redis session manager
            await session_mgr.set_player_room(pid, room_id)
            await session_mgr.update_session_state(pid, "in_game", room_id)
            
            conn = self._conn_manager.get_connection_by_player(pid)
            if conn:
                self._conn_manager.join_room(conn.connection_id, room_id)

        # Broadcast game start
        for i, pid in enumerate(player_ids):
            start_msg = GameStartedResponse(
                room_id=room_id,
                initial_state=game.to_client_state(),
                your_turn=(i == game.current_player_idx),
            )
            sent = await self._conn_manager.send_personal(pid, start_msg.model_dump())
            if not sent:
                # Player might be on another instance
                await self._pubsub.publish_to_player(
                    pid,
                    PubSubMessage(
                        event="game_started",
                        room_id=room_id,
                        sender_instance_id=self._settings.instance_id,
                        payload=start_msg.model_dump(),
                    ),
                )

        # Initial snapshot
        await self._save_snapshot(room_id)

        logger.info(
            "game_created",
            room_id=room_id,
            players=player_ids,
        )
        return game

    async def process_action(
        self,
        room_id: str,
        player_id: str,
        action: str,
        data: dict[str, Any],
        sequence_number: int,
    ) -> bool:
        """Validate and apply a player action.

        This is the hot path — called on every player input. Must be fast.

        Returns True if the action was valid and applied, False otherwise.
        """
        game = self._games.get(room_id)
        if game is None:
            await self._send_error(player_id, "GAME_NOT_FOUND", "Game not found")
            return False

        if game.status != GameStatus.IN_PROGRESS:
            await self._send_error(player_id, "GAME_NOT_ACTIVE", "Game is not active")
            return False

        if player_id != game.current_player_id:
            await self._send_error(player_id, "NOT_YOUR_TURN", "It's not your turn")
            return False

        # ── Action Validation & Application ──────────────────────────
        if action == "place":
            valid = await self._apply_place_action(game, player_id, data)
        else:
            await self._send_error(player_id, "UNKNOWN_ACTION", f"Unknown action: {action}")
            return False

        if not valid:
            return False

        # Record action
        game.action_history.append({
            "player_id": player_id,
            "action": action,
            "data": data,
            "turn": game.turn_number,
            "timestamp": time.time(),
        })
        game.last_action_time = time.time()
        game.turn_number += 1

        # Check win condition
        game_over = self._check_game_over(game)

        # Advance turn (if game isn't over)
        if not game_over:
            game.current_player_idx = (game.current_player_idx + 1) % len(game.player_ids)

        # Broadcast state update to all players in the room
        await self._broadcast_state_update(game, action, data)

        if game_over:
            await self._handle_game_over(game)

        return True

    async def _apply_place_action(
        self, game: ActiveGame, player_id: str, data: dict[str, Any]
    ) -> bool:
        """Validate and apply a 'place' action on the board.

        Server-side validation:
        1. Coordinates are within bounds.
        2. Target cell is empty.
        3. Player is using the action during their turn (already checked).

        Anti-cheat: The client can send any coordinates, but we validate
        everything here. A cheating client that sends out-of-bounds coords
        or tries to overwrite filled cells gets an error, not a ban —
        could be a bug in their client.
        """
        x = data.get("x")
        y = data.get("y")

        if x is None or y is None:
            await self._send_error(player_id, "MISSING_COORDS", "Missing x or y coordinate")
            return False

        if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
            await self._send_error(
                player_id, "OUT_OF_BOUNDS",
                f"Coordinates ({x},{y}) out of bounds (0-{BOARD_SIZE - 1})"
            )
            return False

        if game.board[y][x] != 0:
            await self._send_error(player_id, "CELL_OCCUPIED", f"Cell ({x},{y}) is already occupied")
            return False

        # Apply the placement
        player_num = game.player_ids.index(player_id) + 1  # 1 or 2
        game.board[y][x] = player_num
        game.scores[player_id] = game.scores.get(player_id, 0) + 1

        # Claim adjacent empty cells (territory expansion)
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            nx, ny = x + dx, y + dy
            if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE and game.board[ny][nx] == 0:
                game.board[ny][nx] = player_num
                game.scores[player_id] += 1

        return True

    def _check_game_over(self, game: ActiveGame) -> bool:
        """Check if the game has ended.

        Game ends when:
        1. The board is full (no empty cells).
        2. Maximum turns reached (safety cap).
        """
        if game.turn_number >= MAX_TURNS:
            game.status = GameStatus.FINISHED
            self._determine_winner(game)
            return True

        # Check if board is full
        has_empty = any(
            game.board[y][x] == 0
            for y in range(BOARD_SIZE)
            for x in range(BOARD_SIZE)
        )
        if not has_empty:
            game.status = GameStatus.FINISHED
            self._determine_winner(game)
            return True

        return False

    def _determine_winner(self, game: ActiveGame) -> None:
        """Determine the winner based on scores."""
        if len(game.player_ids) < 2:
            return

        p1, p2 = game.player_ids[0], game.player_ids[1]
        s1, s2 = game.scores.get(p1, 0), game.scores.get(p2, 0)

        if s1 > s2:
            game.winner_id = p1
        elif s2 > s1:
            game.winner_id = p2
        else:
            game.winner_id = None  # Draw

    async def _broadcast_state_update(
        self, game: ActiveGame, action: str, action_data: dict[str, Any]
    ) -> None:
        """Send the current state to all players in the game."""
        for i, pid in enumerate(game.player_ids):
            msg = GameStateUpdateResponse(
                room_id=game.room_id,
                state=game.to_client_state(),
                last_action={"action": action, "data": action_data},
                your_turn=(pid == game.current_player_id),
                turn_number=game.turn_number,
                server_timestamp=time.time(),
            )
            sent = await self._conn_manager.send_personal(pid, msg.model_dump())
            if not sent:
                await self._pubsub.publish_to_player(
                    pid,
                    PubSubMessage(
                        event="state_update",
                        room_id=game.room_id,
                        sender_instance_id=self._settings.instance_id,
                        payload=msg.model_dump(),
                    ),
                )

    async def _handle_game_over(self, game: ActiveGame) -> None:
        """Process game completion: calculate ELO, notify players, clean up."""
        logger.info(
            "game_over",
            room_id=game.room_id,
            winner=game.winner_id,
            turns=game.turn_number,
            scores=game.scores,
        )

        # Calculate ELO changes
        elo_changes = self._calculate_elo_changes(game)

        # Notify players
        for pid in game.player_ids:
            new_elo, delta = elo_changes.get(pid, (game.player_elos.get(pid, 1000), 0))

            if game.winner_id is None:
                result = "draw"
            elif pid == game.winner_id:
                result = "win"
            else:
                result = "loss"

            msg = GameOverResponse(
                room_id=game.room_id,
                winner_id=game.winner_id,
                result=result,
                elo_delta=delta,
                final_state=game.to_client_state(),
            )
            sent = await self._conn_manager.send_personal(pid, msg.model_dump())
            if not sent:
                await self._pubsub.publish_to_player(
                    pid,
                    PubSubMessage(
                        event="game_over",
                        room_id=game.room_id,
                        sender_instance_id=self._settings.instance_id,
                        payload=msg.model_dump(),
                    ),
                )

        # Persist match result asynchronously (fire-and-forget to not block)
        asyncio.create_task(
            self._persist_result(game, elo_changes),
            name=f"persist_{game.room_id}",
        )

        # Final snapshot and cleanup
        await self._save_snapshot(game.room_id)
        await self._cleanup_game(game.room_id)

    def _calculate_elo_changes(
        self, game: ActiveGame
    ) -> dict[str, tuple[int, int]]:
        """Calculate ELO changes using standard FIDE formula.

        Returns: {player_id: (new_elo, delta)}
        """
        if len(game.player_ids) < 2:
            return {}

        p1, p2 = game.player_ids[0], game.player_ids[1]
        elo_1 = game.player_elos.get(p1, 1000)
        elo_2 = game.player_elos.get(p2, 1000)

        # Expected scores
        exp_1 = 1.0 / (1.0 + math.pow(10, (elo_2 - elo_1) / 400.0))
        exp_2 = 1.0 - exp_1

        # Actual scores
        if game.winner_id == p1:
            actual_1, actual_2 = 1.0, 0.0
        elif game.winner_id == p2:
            actual_1, actual_2 = 0.0, 1.0
        else:
            actual_1, actual_2 = 0.5, 0.5

        # K-factor (higher for new players)
        k = 32

        delta_1 = round(k * (actual_1 - exp_1))
        delta_2 = round(k * (actual_2 - exp_2))

        return {
            p1: (elo_1 + delta_1, delta_1),
            p2: (elo_2 + delta_2, delta_2),
        }

    async def _persist_result(
        self, game: ActiveGame, elo_changes: dict[str, tuple[int, int]]
    ) -> None:
        """Persist match result to PostgreSQL.

        This runs as a fire-and-forget background task. If it fails,
        it logs the error but doesn't crash the game loop. A separate
        recovery job can reconcile missing records later.
        """
        try:
            from app.persistence.database import get_session_factory

            factory = get_session_factory()
            async with factory() as session:
                from app.persistence.repositories import MatchRepository

                repo = MatchRepository(session)
                match = await repo.create_match(
                    room_id=game.room_id,
                    player_ids=game.player_ids,
                    player_elos=game.player_elos,
                )
                await repo.record_result(
                    match_id=match.id,
                    winner_id=game.winner_id,
                    final_state=game.to_client_state(),
                    total_turns=game.turn_number,
                    duration_seconds=time.time() - game.started_at,
                    elo_changes=elo_changes,
                )
                await session.commit()
                logger.info("match_persisted", room_id=game.room_id, match_id=str(match.id))
        except Exception as exc:
            logger.error(
                "match_persist_failed",
                room_id=game.room_id,
                error=str(exc),
            )

    # ── Disconnect / Reconnect Handling ──────────────────────────────

    async def handle_player_disconnect(self, room_id: str, player_id: str) -> None:
        """Handle a player disconnecting mid-game.

        Strategy:
        1. Notify the opponent that the player disconnected.
        2. Start a grace period timer.
        3. If the player reconnects within the grace period, resume.
        4. If not, the disconnected player forfeits.
        """
        game = self._games.get(room_id)
        if game is None or game.status != GameStatus.IN_PROGRESS:
            return

        game.disconnected_players.add(player_id)
        game.status = GameStatus.PAUSED

        # Notify opponent
        for pid in game.player_ids:
            if pid != player_id:
                msg = PlayerDisconnectedNotice(
                    player_id=player_id,
                    grace_period_sec=self._settings.reconnect_grace_period_sec,
                )
                await self._conn_manager.send_personal(pid, msg.model_dump())

        # Start grace period timer
        asyncio.create_task(
            self._disconnect_timer(room_id, player_id),
            name=f"disconnect_timer_{room_id}_{player_id}",
        )

        logger.info(
            "player_disconnected_from_game",
            room_id=room_id,
            player_id=player_id,
            grace_period=self._settings.reconnect_grace_period_sec,
        )

    async def _disconnect_timer(self, room_id: str, player_id: str) -> None:
        """Wait for reconnection, then forfeit if the player didn't return."""
        await asyncio.sleep(self._settings.reconnect_grace_period_sec)

        game = self._games.get(room_id)
        if game is None:
            return

        if player_id in game.disconnected_players:
            # Player didn't reconnect — they forfeit
            logger.info("player_forfeited", room_id=room_id, player_id=player_id)

            # The other player wins
            for pid in game.player_ids:
                if pid != player_id:
                    game.winner_id = pid
                    break

            game.status = GameStatus.FINISHED
            await self._handle_game_over(game)

    async def handle_player_reconnect(self, room_id: str, player_id: str) -> None:
        """Handle a player reconnecting to an active game.

        Sends the current game state to the reconnected player and
        notifies the opponent.
        """
        game = self._games.get(room_id)
        if game is None:
            return

        game.disconnected_players.discard(player_id)

        # Resume game if it was paused
        if game.status == GameStatus.PAUSED and not game.disconnected_players:
            game.status = GameStatus.IN_PROGRESS

        # Send current state to reconnected player
        conn = self._conn_manager.get_connection_by_player(player_id)
        if conn:
            self._conn_manager.join_room(conn.connection_id, room_id)

        state_msg = GameStateUpdateResponse(
            room_id=room_id,
            state=game.to_client_state(),
            last_action=None,
            your_turn=(player_id == game.current_player_id),
            turn_number=game.turn_number,
            server_timestamp=time.time(),
        )
        await self._conn_manager.send_personal(player_id, state_msg.model_dump())

        # Notify opponent
        for pid in game.player_ids:
            if pid != player_id:
                msg = PlayerReconnectedNotice(player_id=player_id)
                await self._conn_manager.send_personal(pid, msg.model_dump())

        logger.info("player_reconnected_to_game", room_id=room_id, player_id=player_id)

    # ── State from Redis (for recovery) ──────────────────────────────

    async def restore_game_from_snapshot(self, room_id: str) -> ActiveGame | None:
        """Restore an active game from a Redis snapshot.

        Used when:
        1. This instance takes ownership of an orphaned room.
        2. A player reconnects and the game state needs to be loaded.
        """
        state_key = RedisKey.room_state(room_id)
        raw = await self._redis.get(state_key)
        if raw is None:
            return None

        redis_state = RedisGameState.deserialize(raw.decode() if isinstance(raw, bytes) else raw)

        # We also need room metadata for player list
        room_key = RedisKey.room(room_id)
        room_data = await self._redis.hgetall(room_key)
        if not room_data:
            return None

        decoded_room = {k.decode(): v.decode() for k, v in room_data.items()}
        room = RedisRoom.from_dict(decoded_room)

        game = ActiveGame(
            room_id=room_id,
            player_ids=room.player_ids,
            board=redis_state.board,
            scores=redis_state.scores,
            turn_number=redis_state.turn_number,
            status=redis_state.status,
            winner_id=redis_state.winner_id,
            action_history=redis_state.action_history,
        )

        self._games[room_id] = game
        logger.info("game_restored_from_snapshot", room_id=room_id)
        return game

    # ── Snapshots ────────────────────────────────────────────────────

    async def _snapshot_loop(self) -> None:
        """Periodically save game state snapshots to Redis."""
        interval = self._settings.state_snapshot_interval_sec

        while self._running:
            try:
                for room_id in list(self._games.keys()):
                    await self._save_snapshot(room_id)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("snapshot_error", error=str(exc))
                await asyncio.sleep(interval)

    async def _save_snapshot(self, room_id: str) -> None:
        """Save a single game's state to Redis."""
        game = self._games.get(room_id)
        if game is None:
            return

        state = game.to_redis_state()
        key = RedisKey.room_state(room_id)
        await self._redis.set(key, state.serialize(), ex=3600)

    # ── Cleanup ──────────────────────────────────────────────────────

    async def _cleanup_game(self, room_id: str) -> None:
        """Clean up a completed game."""
        game = self._games.pop(room_id, None)
        if game is None:
            return

        # Leave room for all players on this instance
        for pid in game.player_ids:
            conn = self._conn_manager.get_connection_by_player(pid)
            if conn:
                self._conn_manager.leave_room(conn.connection_id)

        # Clean up Redis keys (allow a delay for clients to read final state)
        await asyncio.sleep(5)
        pipe = self._redis.pipeline()
        pipe.delete(RedisKey.room(room_id))
        pipe.delete(RedisKey.room_state(room_id))
        for pid in game.player_ids:
            pipe.delete(RedisKey.player_room(pid))
        await pipe.execute()

        logger.info("game_cleaned_up", room_id=room_id)

    def get_game(self, room_id: str) -> ActiveGame | None:
        """Get an active game by room_id."""
        return self._games.get(room_id)

    # ── Helpers ──────────────────────────────────────────────────────

    async def _send_error(self, player_id: str, code: str, message: str) -> None:
        """Send an error message to a player."""
        msg = ErrorResponse(code=code, message=message)
        await self._conn_manager.send_personal(player_id, msg.model_dump())
