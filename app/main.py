"""
FastAPI Application Factory.

This module creates and configures the FastAPI application, including:
- Lifespan handler for startup/shutdown
- Middleware registration
- Router inclusion
- CORS configuration

The lifespan pattern (AsyncGenerator) is used over the deprecated
@app.on_event("startup") decorator because:
1. It's the recommended pattern in FastAPI ≥ 0.93.
2. It supports dependency injection and proper error handling.
3. Shutdown code runs even if startup partially fails.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import RateLimitMiddleware
from app.api.rest_routes import router as rest_router
from app.api.ws_routes import router as ws_router
from app.config import get_settings
from app.dependencies import init_dependencies, shutdown_dependencies
from app.persistence.database import close_db, init_db
from app.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler.

    Startup sequence:
    1. Logging (first, so everything else can log)
    2. Database (needed by REST endpoints)
    3. Dependencies (Redis, services, background tasks)
    4. Instance heartbeat loop

    Shutdown sequence (reverse):
    1. Cancel heartbeat
    2. Stop all services
    3. Close database
    """
    settings = get_settings()

    # ── Startup ──────────────────────────────────────────────────────
    setup_logging()
    logger.info(
        "application_starting",
        instance_id=settings.instance_id,
        environment=settings.app_env,
    )

    # Initialize database
    try:
        await init_db()
    except Exception as exc:
        logger.error("database_init_failed", error=str(exc))
        # Continue without DB — WS functionality still works
        # PostgreSQL errors shouldn't prevent the game server from starting

    # Initialize all shared services
    await init_dependencies()

    # Start instance heartbeat
    heartbeat_task = asyncio.create_task(
        _instance_heartbeat_loop(), name="instance_heartbeat"
    )

    logger.info(
        "application_started",
        instance_id=settings.instance_id,
        host=settings.app_host,
        port=settings.app_port,
    )

    yield  # ← Application runs here

    # ── Shutdown ─────────────────────────────────────────────────────
    logger.info("application_shutting_down")

    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    await shutdown_dependencies()
    await close_db()

    logger.info("application_stopped")


async def _instance_heartbeat_loop() -> None:
    """Periodically register this instance's heartbeat in Redis.

    Other instances check this to detect dead instances and claim
    their orphaned rooms. If this instance's heartbeat expires,
    another instance knows it crashed and can take over.
    """
    from app.dependencies import get_session_manager

    settings = get_settings()

    while True:
        try:
            session_mgr = get_session_manager()
            await session_mgr.register_instance_heartbeat()
            await asyncio.sleep(settings.instance_heartbeat_interval_sec)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("heartbeat_error", error=str(exc))
            await asyncio.sleep(5)


def create_app() -> FastAPI:
    """Factory function for the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Real-Time Multiplayer Game Backend",
        description="Production-grade WebSocket game server with matchmaking, "
                    "server-authoritative state, and horizontal scalability.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
    )

    # ── CORS ─────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
            "http://127.0.0.1:5500",
            "http://localhost:5500",
            "*"  # Add * as fallback for debugging (browsers may ignore it with allow_credentials)
        ] if not settings.is_production else ["http://localhost"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Rate Limiting ────────────────────────────────────────────────
    app.add_middleware(RateLimitMiddleware)

    # ── Routers ──────────────────────────────────────────────────────
    app.include_router(ws_router)
    app.include_router(rest_router)

    return app


# Application instance — imported by uvicorn
app = create_app()
