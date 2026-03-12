"""Redis client lifecycle helpers."""

import logging

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import RedisError

from core.config import get_settings


logger = logging.getLogger(__name__)

settings = get_settings()
_redis_pool: ConnectionPool | None = None
_redis_client: Redis | None = None


async def init_redis() -> Redis | None:
    """Initialize a shared Redis client backed by one connection pool."""

    global _redis_pool, _redis_client

    if not settings.redis_enabled:
        return None

    if _redis_client is None:
        pool_kwargs = {
            "host": settings.redis_host,
            "port": settings.redis_port,
            "db": settings.redis_db,
            "decode_responses": True,
        }
        if settings.redis_password:
            pool_kwargs["password"] = settings.redis_password

        _redis_pool = ConnectionPool(**pool_kwargs)
        _redis_client = Redis(connection_pool=_redis_pool)

    try:
        await _redis_client.ping()
    except RedisError:
        logger.exception("redis_connection_failed")
        raise

    return _redis_client


def get_redis_client() -> Redis | None:
    """Return the shared Redis client if it has been initialized."""

    return _redis_client


async def close_redis() -> None:
    """Close the shared Redis client and pool."""

    global _redis_pool, _redis_client

    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None

    if _redis_pool is not None:
        await _redis_pool.disconnect()
        _redis_pool = None
