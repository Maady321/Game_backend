"""
Rate limiting and authentication middleware.

Rate Limiting Strategy:
- Sliding window using Redis sorted sets. More accurate than fixed-window
  counters (no burst at window boundaries) and simpler than token bucket.
- WS messages: per-player, per-second limit. Prevents spam flooding.
- REST endpoints: per-IP, per-minute limit. Prevents enumeration/abuse.

Why Redis for rate limiting (instead of in-memory)?
- Consistent across all instances. A player can't bypass limits by
  reconnecting to a different instance.
- Survives restarts. Limits persist even if an instance crashes.
"""

from __future__ import annotations

import time
from typing import Any

import redis.asyncio as aioredis
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_settings
from app.utils.constants import RedisKey
from app.utils.logging import get_logger

logger = get_logger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """HTTP rate limiting middleware using Redis sliding window."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # Skip rate limiting for WebSocket upgrades (handled separately)
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # Skip health check
        if request.url.path in ("/health", "/docs", "/openapi.json"):
            return await call_next(request)

        settings = get_settings()

        # Get client identifier
        client_ip = request.client.host if request.client else "unknown"

        try:
            from app.dependencies import get_redis
            redis_pool = get_redis()

            allowed = await check_rate_limit(
                redis_pool=redis_pool,
                identifier=f"rest:{client_ip}",
                max_requests=settings.rate_limit_rest_requests_per_min,
                window_seconds=60,
            )

            if not allowed:
                logger.warning("rate_limited", client_ip=client_ip, path=request.url.path)
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again later."},
                    headers={"Retry-After": "60"},
                )
        except RuntimeError:
            # Redis not initialized yet (during startup)
            pass

        response = await call_next(request)
        return response


async def check_rate_limit(
    redis_pool: aioredis.Redis,
    identifier: str,
    max_requests: int,
    window_seconds: int,
) -> bool:
    """Sliding window rate limiter using Redis sorted sets.

    How it works:
    1. Each request adds a timestamp to a sorted set keyed by identifier.
    2. Remove all entries older than the window.
    3. Count remaining entries.
    4. If count > max_requests, reject.

    The sorted set automatically cleans up old entries on each check,
    and the TTL ensures the key is garbage-collected if the client stops.

    Args:
        redis_pool: Redis connection.
        identifier: Unique key (e.g., "ws:player_id" or "rest:ip_addr").
        max_requests: Max allowed requests in the window.
        window_seconds: Window size in seconds.

    Returns:
        True if the request is allowed, False if rate-limited.
    """
    now = time.time()
    window_start = now - window_seconds
    key = f"rl:{identifier}"

    pipe = redis_pool.pipeline()
    # Remove expired entries
    pipe.zremrangebyscore(key, "-inf", window_start)
    # Add current request
    pipe.zadd(key, {f"{now}:{id(now)}": now})
    # Count requests in window
    pipe.zcard(key)
    # Set TTL so keys don't persist forever
    pipe.expire(key, window_seconds + 1)

    results = await pipe.execute()
    request_count = results[2]

    return request_count <= max_requests


async def check_ws_rate_limit(
    redis_pool: aioredis.Redis,
    player_id: str,
    max_per_sec: int,
) -> bool:
    """Check WebSocket message rate limit for a specific player."""
    return await check_rate_limit(
        redis_pool=redis_pool,
        identifier=f"ws:{player_id}",
        max_requests=max_per_sec,
        window_seconds=1,
    )
