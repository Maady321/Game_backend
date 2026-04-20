"""
SQLAlchemy ORM models for PostgreSQL persistence.

Design decisions:
- Using SQLAlchemy 2.0 Mapped-column style for type safety.
- UUIDs as primary keys instead of auto-increment integers. Reasons:
  1. No sequential guessing / enumeration attacks.
  2. Can be generated client-side or by any instance without coordination.
  3. Safe for distributed/sharded setups.
- Timestamps use timezone-aware UTC. Never store naive datetimes.
- Indexes on columns used in WHERE/JOIN/ORDER BY (elo_rating, status,
  created_at). Missing indexes are the #1 performance killer in production.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


class Player(Base):
    """Persistent player profile and credentials.

    This is the cold-state representation. Hot-state (connection info,
    current room) lives in Redis.
    """

    __tablename__ = "players"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # ── Game stats ───────────────────────────────────────────────────
    elo_rating: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    games_played: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    games_won: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    games_lost: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    games_drawn: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Account state ────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Timestamps ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relationships ────────────────────────────────────────────────
    match_participations: Mapped[list[MatchParticipant]] = relationship(
        back_populates="player", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_players_elo_rating", "elo_rating"),
        Index("ix_players_active_elo", "is_active", "elo_rating"),
    )

    def __repr__(self) -> str:
        return f"<Player {self.username} elo={self.elo_rating}>"


class Match(Base):
    """Record of a completed (or in-progress) game match.

    One Match ↔ many MatchParticipants (typically 2 for 1v1).
    The full game replay is stored in `game_log` as JSONB —
    compact, queryable, and doesn't require a separate table.
    """

    __tablename__ = "matches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    room_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="in_progress", index=True
    )
    winner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), nullable=True
    )

    # ── Game data ────────────────────────────────────────────────────
    total_turns: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    game_log: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    final_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relationships ────────────────────────────────────────────────
    participants: Mapped[list[MatchParticipant]] = relationship(
        back_populates="match", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_matches_status_created", "status", "created_at"),
    )


class MatchParticipant(Base):
    """Junction table between Match and Player.

    Stores per-player match outcome data. Separating this from Match
    allows clean N-player support and per-player ELO deltas.
    """

    __tablename__ = "match_participants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )

    # ── Outcome ──────────────────────────────────────────────────────
    result: Mapped[str | None] = mapped_column(
        String(10), nullable=True  # "win", "loss", "draw", null=in-progress
    )
    elo_before: Mapped[int] = mapped_column(Integer, nullable=False)
    elo_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elo_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Relationships ────────────────────────────────────────────────
    match: Mapped[Match] = relationship(back_populates="participants")
    player: Mapped[Player] = relationship(back_populates="match_participations")

    __table_args__ = (
        Index("ix_mp_player_match", "player_id", "match_id", unique=True),
    )
