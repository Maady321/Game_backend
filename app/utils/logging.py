"""
Structured logging configuration using structlog.

Why structlog over stdlib logging?
- JSON output in production → parseable by ELK/Datadog/CloudWatch
- Human-readable colored output in development
- Automatic context binding (request_id, player_id, room_id) without
  threading ThreadLocals through every function signature
- Zero-cost if a log level is disabled (lazy evaluation)
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.config import get_settings


def setup_logging() -> None:
    """Configure structlog + stdlib logging bridge.

    Called once at application startup in the lifespan handler.
    """
    settings = get_settings()
    log_level = logging.DEBUG if settings.app_debug else logging.INFO

    # ── Shared processors (run for every log event) ──────────────────
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.is_production:
        # JSON lines for machine consumption
        renderer = structlog.processors.JSONRenderer()
    else:
        # Colored, padded output for human eyes
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging so that uvicorn/sqlalchemy logs also go
    # through structlog's formatting pipeline.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Quiet down noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a named, bound logger instance.

    Usage:
        logger = get_logger(__name__)
        logger.info("player_connected", player_id="abc123")
    """
    return structlog.get_logger(name)
