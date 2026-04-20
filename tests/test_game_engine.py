"""
Tests for the Game State Engine.

Tests the server-authoritative game logic: action validation,
state transitions, win conditions, and ELO calculations.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.game_engine import ActiveGame, GameEngine
from app.utils.constants import BOARD_SIZE, GameStatus


@pytest.fixture
def game():
    """Create a fresh ActiveGame for testing."""
    return ActiveGame(
        room_id="room-test-123",
        player_ids=["player-1", "player-2"],
        player_elos={"player-1": 1200, "player-2": 1000},
        scores={"player-1": 0, "player-2": 0},
        status=GameStatus.IN_PROGRESS,
    )


@pytest.fixture
def engine(redis_pool, connection_manager):
    """Create a GameEngine with mocked dependencies."""
    pubsub = MagicMock()
    pubsub.publish_to_player = AsyncMock()
    return GameEngine(redis_pool, connection_manager, pubsub)


class TestActiveGame:
    """Test the ActiveGame data model."""

    def test_current_player(self, game):
        assert game.current_player_id == "player-1"
        game.current_player_idx = 1
        assert game.current_player_id == "player-2"

    def test_to_client_state(self, game):
        state = game.to_client_state()
        assert "board" in state
        assert "scores" in state
        assert "current_player_id" in state
        assert state["player_ids"] == ["player-1", "player-2"]

    def test_to_redis_state(self, game):
        redis_state = game.to_redis_state()
        assert redis_state.room_id == game.room_id
        assert redis_state.turn_number == 0
        assert redis_state.status == "in_progress"


class TestActionValidation:
    """Test server-side action validation."""

    @pytest.mark.asyncio
    async def test_valid_placement(self, engine, game):
        engine._games["room-test-123"] = game

        valid = await engine._apply_place_action(game, "player-1", {"x": 5, "y": 5})
        assert valid is True
        assert game.board[5][5] == 1  # player-1 is player_num 1
        assert game.scores["player-1"] > 0

    @pytest.mark.asyncio
    async def test_out_of_bounds(self, engine, game):
        engine._games["room-test-123"] = game

        valid = await engine._apply_place_action(game, "player-1", {"x": -1, "y": 5})
        assert valid is False

        valid = await engine._apply_place_action(game, "player-1", {"x": 5, "y": BOARD_SIZE})
        assert valid is False

    @pytest.mark.asyncio
    async def test_occupied_cell(self, engine, game):
        engine._games["room-test-123"] = game

        game.board[3][3] = 1  # Pre-occupy
        valid = await engine._apply_place_action(game, "player-1", {"x": 3, "y": 3})
        assert valid is False

    @pytest.mark.asyncio
    async def test_missing_coordinates(self, engine, game):
        engine._games["room-test-123"] = game

        valid = await engine._apply_place_action(game, "player-1", {})
        assert valid is False

    @pytest.mark.asyncio
    async def test_adjacent_territory_claim(self, engine, game):
        """Placing a piece should claim adjacent empty cells."""
        engine._games["room-test-123"] = game

        # Place in the middle of the board (4 adjacent empty cells)
        valid = await engine._apply_place_action(game, "player-1", {"x": 5, "y": 5})
        assert valid is True

        # The placed cell + up to 4 adjacent cells should be claimed
        assert game.board[5][5] == 1
        # At least the piece itself should count
        assert game.scores["player-1"] >= 1

    @pytest.mark.asyncio
    async def test_corner_placement_claims_less(self, engine, game):
        """Corner placement should only claim 2 adjacent cells max."""
        engine._games["room-test-123"] = game

        valid = await engine._apply_place_action(game, "player-1", {"x": 0, "y": 0})
        assert valid is True
        # Corner: 1 piece + 2 adjacent = 3 max
        assert game.scores["player-1"] <= 3


class TestTurnManagement:
    """Test turn progression."""

    @pytest.mark.asyncio
    async def test_process_action_wrong_turn(self, engine, game):
        engine._games["room-test-123"] = game

        # It's player-1's turn, but player-2 tries to act
        result = await engine.process_action(
            "room-test-123", "player-2", "place", {"x": 0, "y": 0}, 0
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_process_action_advances_turn(self, engine, game):
        engine._games["room-test-123"] = game
        assert game.current_player_id == "player-1"

        result = await engine.process_action(
            "room-test-123", "player-1", "place", {"x": 0, "y": 0}, 0
        )
        assert result is True
        assert game.current_player_id == "player-2"

    @pytest.mark.asyncio
    async def test_unknown_action(self, engine, game):
        engine._games["room-test-123"] = game

        result = await engine.process_action(
            "room-test-123", "player-1", "teleport", {}, 0
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_action_on_nonexistent_room(self, engine):
        result = await engine.process_action(
            "room-fake", "player-1", "place", {"x": 0, "y": 0}, 0
        )
        assert result is False


class TestWinConditions:
    """Test game-over detection and winner determination."""

    def test_full_board_ends_game(self, engine, game):
        # Fill the entire board
        for y in range(BOARD_SIZE):
            for x in range(BOARD_SIZE):
                game.board[y][x] = 1 if (x + y) % 2 == 0 else 2

        result = engine._check_game_over(game)
        assert result is True
        assert game.status == GameStatus.FINISHED

    def test_max_turns_ends_game(self, engine, game):
        from app.utils.constants import MAX_TURNS

        game.turn_number = MAX_TURNS
        result = engine._check_game_over(game)
        assert result is True
        assert game.status == GameStatus.FINISHED

    def test_winner_by_score(self, game):
        from app.core.game_engine import GameEngine
        game.scores = {"player-1": 60, "player-2": 40}

        engine = GameEngine.__new__(GameEngine)
        engine._determine_winner(game)
        assert game.winner_id == "player-1"

    def test_draw(self, game):
        from app.core.game_engine import GameEngine
        game.scores = {"player-1": 50, "player-2": 50}

        engine = GameEngine.__new__(GameEngine)
        engine._determine_winner(game)
        assert game.winner_id is None


class TestELO:
    """Test ELO rating calculations."""

    def test_winner_gains_elo(self, engine, game):
        game.winner_id = "player-1"
        changes = engine._calculate_elo_changes(game)

        new_elo_1, delta_1 = changes["player-1"]
        new_elo_2, delta_2 = changes["player-2"]

        assert delta_1 > 0  # Winner gains
        assert delta_2 < 0  # Loser loses
        assert delta_1 + delta_2 == 0  # Zero-sum (approximately)

    def test_draw_elo(self, engine, game):
        game.winner_id = None
        changes = engine._calculate_elo_changes(game)

        # Higher-rated player should lose ELO in a draw
        _, delta_1 = changes["player-1"]  # 1200
        _, delta_2 = changes["player-2"]  # 1000

        assert delta_1 < 0  # Higher rated loses on draw
        assert delta_2 > 0  # Lower rated gains on draw

    def test_upset_gives_more_elo(self, engine):
        """An upset (lower-rated player wins) should give more ELO."""
        game_normal = ActiveGame(
            room_id="r1",
            player_ids=["p1", "p2"],
            player_elos={"p1": 1200, "p2": 1000},
            scores={"p1": 0, "p2": 0},
        )
        game_normal.winner_id = "p1"  # Expected winner

        game_upset = ActiveGame(
            room_id="r2",
            player_ids=["p1", "p2"],
            player_elos={"p1": 1200, "p2": 1000},
            scores={"p1": 0, "p2": 0},
        )
        game_upset.winner_id = "p2"  # Upset

        normal_changes = engine._calculate_elo_changes(game_normal)
        upset_changes = engine._calculate_elo_changes(game_upset)

        # The upset winner should gain more than the expected winner
        _, normal_gain = normal_changes["p1"]
        _, upset_gain = upset_changes["p2"]
        assert upset_gain > normal_gain


class TestDisconnectHandling:
    """Test player disconnect/reconnect during a game."""

    @pytest.mark.asyncio
    async def test_disconnect_pauses_game(self, engine, game):
        engine._games["room-test-123"] = game

        await engine.handle_player_disconnect("room-test-123", "player-1")

        assert game.status == GameStatus.PAUSED
        assert "player-1" in game.disconnected_players

    @pytest.mark.asyncio
    async def test_reconnect_resumes_game(self, engine, game):
        engine._games["room-test-123"] = game
        game.status = GameStatus.PAUSED
        game.disconnected_players.add("player-1")

        await engine.handle_player_reconnect("room-test-123", "player-1")

        assert game.status == GameStatus.IN_PROGRESS
        assert "player-1" not in game.disconnected_players
