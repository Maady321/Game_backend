"""
Data Access Layer (Repository Pattern).

Why repository pattern?
- Decouples business logic from SQL specifics. The game engine calls
  `repo.record_match_result()`, not raw SQL — making it testable with
  simple mocks and swappable for different backends.
- Centralizes query logic — no scattered SELECT/INSERT across services.
- Async all the way — no blocking the event loop.

Each method is a self-contained unit of work. For operations that span
multiple tables (e.g., recording a match result + updating player ELO),
we use a single session with explicit transaction boundaries.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Match, MatchParticipant, Player
from app.utils.logging import get_logger

logger = get_logger(__name__)


class PlayerRepository:
    """CRUD operations for Player records."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, username: str, email: str, password_hash: str) -> Player:
        """Register a new player with default ELO."""
        player = Player(
            username=username,
            email=email,
            password_hash=password_hash,
        )
        self._session.add(player)
        await self._session.flush()  # Populate id without committing
        logger.info("player_created", player_id=str(player.id), username=username)
        return player

    async def get_by_id(self, player_id: uuid.UUID) -> Player | None:
        result = await self._session.execute(
            select(Player).where(Player.id == player_id)
        )
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> Player | None:
        result = await self._session.execute(
            select(Player).where(Player.username == username)
        )
        return result.scalar_one_or_none()

    async def update_login_timestamp(self, player_id: uuid.UUID) -> None:
        await self._session.execute(
            update(Player)
            .where(Player.id == player_id)
            .values(last_login_at=datetime.now(timezone.utc))
        )

    async def update_elo(
        self, player_id: uuid.UUID, new_elo: int, result: str
    ) -> None:
        """Update player ELO and game counters atomically.

        Args:
            player_id: Player to update.
            new_elo: New ELO rating after the match.
            result: One of "win", "loss", "draw".
        """
        increment_fields: dict[str, Any] = {"elo_rating": new_elo, "games_played": Player.games_played + 1}

        if result == "win":
            increment_fields["games_won"] = Player.games_won + 1
        elif result == "loss":
            increment_fields["games_lost"] = Player.games_lost + 1
        elif result == "draw":
            increment_fields["games_drawn"] = Player.games_drawn + 1

        await self._session.execute(
            update(Player)
            .where(Player.id == player_id)
            .values(**increment_fields)
        )

    async def get_leaderboard(
        self, page: int = 1, page_size: int = 50
    ) -> tuple[list[Player], int]:
        """Fetch paginated leaderboard sorted by ELO descending.

        Returns:
            Tuple of (players list, total count).
        """
        # Count query
        count_result = await self._session.execute(
            select(func.count()).select_from(Player).where(Player.is_active.is_(True))
        )
        total = count_result.scalar_one()

        # Data query with offset-based pagination
        offset = (page - 1) * page_size
        result = await self._session.execute(
            select(Player)
            .where(Player.is_active.is_(True))
            .order_by(desc(Player.elo_rating))
            .offset(offset)
            .limit(page_size)
        )
        players = list(result.scalars().all())
        return players, total


class MatchRepository:
    """CRUD operations for Match and MatchParticipant records."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_match(
        self,
        room_id: str,
        player_ids: list[str],
        player_elos: dict[str, int],
    ) -> Match:
        """Create a new match record with participant entries.

        Called when a game starts — not when matchmaking finds a pair.
        The match starts as 'in_progress' and is updated when the game ends.
        """
        match = Match(room_id=room_id, status="in_progress", started_at=datetime.now(timezone.utc))
        self._session.add(match)
        await self._session.flush()

        for pid in player_ids:
            participant = MatchParticipant(
                match_id=match.id,
                player_id=uuid.UUID(pid),
                elo_before=player_elos.get(pid, 1000),
            )
            self._session.add(participant)

        await self._session.flush()
        logger.info(
            "match_created",
            match_id=str(match.id),
            room_id=room_id,
            players=player_ids,
        )
        return match

    async def record_result(
        self,
        match_id: uuid.UUID,
        winner_id: str | None,
        final_state: dict[str, Any],
        total_turns: int,
        duration_seconds: float,
        elo_changes: dict[str, tuple[int, int]],  # player_id -> (new_elo, delta)
    ) -> None:
        """Record the outcome of a completed match.

        This is called asynchronously after the game engine declares a
        winner. Updates:
        1. Match record (winner, final state, timing)
        2. MatchParticipant records (result, ELO delta)
        3. Player records (new ELO, game counters)

        All updates happen in one transaction — either all succeed or
        all roll back. This prevents inconsistent state where ELO is
        updated but the match record isn't.
        """
        now = datetime.now(timezone.utc)

        # Update match
        await self._session.execute(
            update(Match)
            .where(Match.id == match_id)
            .values(
                status="finished",
                winner_id=uuid.UUID(winner_id) if winner_id else None,
                total_turns=total_turns,
                duration_seconds=duration_seconds,
                ended_at=now,
                final_state=final_state,
            )
        )

        # Update participants and players
        player_repo = PlayerRepository(self._session)
        for player_id_str, (new_elo, delta) in elo_changes.items():
            player_uuid = uuid.UUID(player_id_str)

            if winner_id is None:
                result = "draw"
            elif player_id_str == winner_id:
                result = "win"
            else:
                result = "loss"

            # Update participant
            await self._session.execute(
                update(MatchParticipant)
                .where(
                    MatchParticipant.match_id == match_id,
                    MatchParticipant.player_id == player_uuid,
                )
                .values(result=result, elo_after=new_elo, elo_delta=delta)
            )

            # Update player
            await player_repo.update_elo(player_uuid, new_elo, result)

        logger.info(
            "match_result_recorded",
            match_id=str(match_id),
            winner_id=winner_id,
            total_turns=total_turns,
        )

    async def get_player_match_history(
        self, player_id: uuid.UUID, limit: int = 20
    ) -> list[Match]:
        """Get recent matches for a player, newest first."""
        result = await self._session.execute(
            select(Match)
            .join(MatchParticipant)
            .where(MatchParticipant.player_id == player_id)
            .order_by(desc(Match.created_at))
            .limit(limit)
        )
        return list(result.scalars().all())
