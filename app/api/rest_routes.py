"""
REST API endpoints for non-real-time operations.

These handle:
- Player registration and login (returns JWT for WS auth)
- Player profile and stats
- Leaderboard
- Server health check

All endpoints are async and use dependency injection for DB sessions.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_connection_manager, get_game_engine, get_matchmaking_service
from app.models.schemas import (
    LeaderboardEntry,
    LeaderboardResponse,
    PlayerLoginRequest,
    PlayerLoginResponse,
    PlayerProfileResponse,
    PlayerRegisterRequest,
)
from app.persistence.database import get_db_session
from app.persistence.repositories import PlayerRepository
from app.utils.auth import create_access_token
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["api"])

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@router.post("/register", response_model=PlayerLoginResponse, status_code=201)
async def register_player(
    request: PlayerRegisterRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Register a new player account.

    Returns a JWT token on successful registration (auto-login).
    """
    repo = PlayerRepository(db)

    # Check for existing username
    existing = await repo.get_by_username(request.username)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        )

    # Create player
    password_hash = pwd_context.hash(request.password)
    player = await repo.create(
        username=request.username,
        email=request.email,
        password_hash=password_hash,
    )
    await db.commit()

    # Generate JWT
    token = create_access_token(str(player.id))

    logger.info("player_registered", player_id=str(player.id), username=request.username)

    return {
        "access_token": token,
        "token_type": "bearer",
        "player_id": str(player.id),
        "username": player.username,
        "elo_rating": player.elo_rating,
    }


@router.post("/login", response_model=PlayerLoginResponse)
async def login_player(
    request: PlayerLoginRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Authenticate a player and return a JWT."""
    repo = PlayerRepository(db)

    player = await repo.get_by_username(request.username)
    if not player or not pwd_context.verify(request.password, player.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    if player.is_banned:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is banned",
        )

    await repo.update_login_timestamp(player.id)
    await db.commit()

    token = create_access_token(str(player.id))

    logger.info("player_logged_in", player_id=str(player.id))

    return {
        "access_token": token,
        "token_type": "bearer",
        "player_id": str(player.id),
        "username": player.username,
        "elo_rating": player.elo_rating,
    }


@router.get("/profile/{player_id}", response_model=PlayerProfileResponse)
async def get_player_profile(
    player_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get a player's public profile and stats."""
    repo = PlayerRepository(db)

    try:
        player_uuid = uuid.UUID(player_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid player ID format")

    player = await repo.get_by_id(player_uuid)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    total = player.games_played or 1  # Prevent division by zero
    win_rate = round(player.games_won / total * 100, 1)

    return {
        "player_id": str(player.id),
        "username": player.username,
        "elo_rating": player.elo_rating,
        "games_played": player.games_played,
        "games_won": player.games_won,
        "games_lost": player.games_lost,
        "games_drawn": player.games_drawn,
        "win_rate": win_rate,
        "created_at": player.created_at,
    }


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get the global leaderboard sorted by ELO."""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 50

    repo = PlayerRepository(db)
    players, total = await repo.get_leaderboard(page=page, page_size=page_size)

    entries = []
    for i, player in enumerate(players):
        rank = (page - 1) * page_size + i + 1
        total_games = player.games_played or 1
        entries.append(
            LeaderboardEntry(
                rank=rank,
                player_id=str(player.id),
                username=player.username,
                elo_rating=player.elo_rating,
                games_played=player.games_played,
                win_rate=round(player.games_won / total_games * 100, 1),
            ).model_dump()
        )

    return {
        "entries": entries,
        "total_players": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check endpoint for load balancer probes.

    Returns service status and basic metrics.
    """
    settings = get_settings()

    try:
        conn_mgr = get_connection_manager()
        game_engine = get_game_engine()
        mm = get_matchmaking_service()

        return {
            "status": "healthy",
            "instance_id": settings.instance_id,
            "active_connections": conn_mgr.active_connection_count,
            "active_games": game_engine.active_game_count,
            "matchmaking_queue_size": await mm.get_queue_size(),
            "environment": settings.app_env,
        }
    except RuntimeError:
        return {"status": "starting", "instance_id": settings.instance_id}
