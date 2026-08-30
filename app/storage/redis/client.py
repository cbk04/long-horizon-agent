"""Redis connection and helper utilities."""

from __future__ import annotations

import redis

from app.config import get_settings

settings = get_settings()

# Synchronous Redis client for worker / harness operations
# protocol=2: force RESP2 for compatibility with Redis 6.x/7.x/8.x
redis_client: redis.Redis = redis.Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    protocol=2,
    socket_timeout=10,
    socket_connect_timeout=10,
    health_check_interval=30,
)


def get_redis() -> redis.Redis:
    """FastAPI dependency: return the shared Redis client."""
    return redis_client
