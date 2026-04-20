"""
Centralized configuration via Pydantic Settings.

All configuration is loaded from environment variables (or .env file).
Pydantic validates types at startup — if any required var is missing or
malformed, the app fails fast with a clear error instead of silently
running with bad config.
"""

from __future__ import annotations

import os
import uuid
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────
    app_env: str = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    # Unique identifier for this running instance — critical for distributed
    # coordination. Auto-generated if not set so that docker-compose scale
    # works without manual config per replica.
    instance_id: str = f"inst-{uuid.uuid4().hex[:8]}"

    # ── Security / JWT ───────────────────────────────────────────────────
    jwt_secret_key: str = "CHANGE-ME"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 1440

    # ── PostgreSQL ───────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://gameuser:gamepass@localhost:5432/gamedb"
    database_pool_size: int = 20
    database_max_overflow: int = 10

    # ── Redis ────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50

    # ── Game Tuning ──────────────────────────────────────────────────────
    game_tick_rate_ms: int = 50
    matchmaking_interval_ms: int = 500
    elo_initial: int = 1000
    elo_tolerance_base: int = 100
    elo_tolerance_expansion_per_sec: int = 10
    elo_tolerance_max: int = 500
    max_matchmaking_wait_sec: int = 120

    # ── Rate Limiting ────────────────────────────────────────────────────
    rate_limit_ws_messages_per_sec: int = 30
    rate_limit_rest_requests_per_min: int = 60

    # ── Session / Reconnection ───────────────────────────────────────────
    session_ttl_sec: int = 3600
    reconnect_grace_period_sec: int = 30
    heartbeat_interval_sec: int = 10
    heartbeat_timeout_sec: int = 30

    # ── State Snapshots ──────────────────────────────────────────────────
    state_snapshot_interval_sec: int = 2
    instance_heartbeat_interval_sec: int = 5
    instance_heartbeat_ttl_sec: int = 15

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings accessor.

    Using lru_cache ensures the .env file is parsed exactly once.
    Every call to get_settings() returns the same frozen object.
    """
    return Settings()
