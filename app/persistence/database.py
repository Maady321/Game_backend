"""
Async SQLAlchemy engine and session factory.

Design decisions:
- create_async_engine with connection pooling. Pool is configured via
  settings so it can be tuned per-environment (dev: small pool,
  prod: larger pool with overflow).
- Session factory uses expire_on_commit=False so that ORM objects remain
  usable after commit without triggering lazy loads (which would fail
  outside an async context).
- Engine lifecycle is managed by the FastAPI lifespan handler —
  pool is disposed on shutdown to cleanly release all connections.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level singletons, initialized by init_db()
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    """Initialize the async engine and session factory.

    Called once during application startup (lifespan handler).
    """
    global _engine, _session_factory
    settings = get_settings()

    _engine = create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,  # Detect stale connections before use
        pool_recycle=3600,    # Recycle connections every hour (avoid PG timeout)
        echo=settings.app_debug,
    )

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Automatically create tables if they do not exist (useful since Alembic wasn't fully initialized)
    from app.models.database import Base
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("database_initialized", pool_size=settings.database_pool_size)


async def close_db() -> None:
    """Dispose engine and release all pooled connections.

    Called during application shutdown.
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("database_connections_closed")
    _engine = None
    _session_factory = None


def get_engine() -> AsyncEngine:
    """Get the initialized engine. Raises if init_db() hasn't been called."""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get the session factory. Raises if init_db() hasn't been called."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


async def get_db_session() -> AsyncSession:
    """Dependency-injectable session generator.

    Usage in FastAPI:
        @router.get("/")
        async def endpoint(db: AsyncSession = Depends(get_db_session)):
            ...

    The session is automatically closed when the request ends.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session  # type: ignore[misc]
            await session.commit()
        except Exception:
            await session.rollback()
            raise
