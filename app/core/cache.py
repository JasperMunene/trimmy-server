"""A small read cache for public, read-mostly responses (search cards, business profiles).

Values are JSON, kept in Redis (CACHE_BACKEND=redis) so every API process shares them, or in this
process (local: tests, a single process). Never required: if Redis is down or slow, reads go to the
database as before, and Redis is skipped for a while instead of being waited on every request.

Freshness: changes clear the affected keys as they're committed (cache_invalidation.py); the TTLs
are only the backstop for anything that slips past.

Keys: c:v1:... (bump v1 when a cached shape changes, so a deploy never reads old shapes).
Profiles also carry a generation number, bumped for changes that can't be traced to one business
(bulk UPDATE/DELETE statements); that retires every cached profile at once.
"""
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

PREFIX = "c:v1:"
CARDS_KEY = PREFIX + "cards"
GEN_KEY = PREFIX + "gen"

_redis = None
_down_until = 0.0
_local: dict[str, tuple[float, str]] = {}  # key -> (expires at, json)


def profile_key(gen: int, business_id: str) -> str:
    return f"{PREFIX}profile:{gen}:{business_id}"


def _client():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5,
                                   health_check_interval=30)
    return _redis


def _backend() -> str:
    return settings.CACHE_BACKEND if settings.REDIS_URL or settings.CACHE_BACKEND != "redis" else "off"


def _usable() -> bool:
    return _backend() != "off" and (_backend() == "local" or time.monotonic() >= _down_until)


def _failed(err: Exception) -> None:
    global _down_until
    _down_until = time.monotonic() + 15
    logger.warning("Cache unavailable, reading from the database for now: %s", err)


async def _get_raw(*keys: str) -> list[Optional[str]]:
    if _backend() == "local":
        now = time.monotonic()
        return [v[1] if (v := _local.get(k)) and v[0] > now else None for k in keys]
    return await _client().mget(keys)


async def get(key: str) -> Any:
    if not _usable():
        return None
    try:
        raw = (await _get_raw(key))[0]
    except Exception as err:
        _failed(err)
        return None
    return json.loads(raw) if raw is not None else None


async def put(key: str, value: Any, ttl: int) -> None:
    if not _usable():
        return
    raw = json.dumps(value, separators=(",", ":"), default=str)
    try:
        if _backend() == "local":
            _local[key] = (time.monotonic() + ttl, raw)
        else:
            await _client().set(key, raw, ex=ttl)
    except Exception as err:
        _failed(err)


async def delete(*keys: str) -> None:
    if not keys or not _usable():
        return
    try:
        if _backend() == "local":
            for k in keys:
                _local.pop(k, None)
        else:
            await _client().delete(*keys)
    except Exception as err:
        _failed(err)


async def generation() -> int:
    """The current profile generation (0 if never bumped or the cache is unavailable)."""
    if not _usable():
        return 0
    try:
        raw = (await _get_raw(GEN_KEY))[0]
    except Exception as err:
        _failed(err)
        return 0
    return int(raw or 0)


async def bump_generation() -> None:
    if not _usable():
        return
    try:
        if _backend() == "local":
            gen = int(_local.get(GEN_KEY, (0, "0"))[1]) + 1
            _local[GEN_KEY] = (float("inf"), str(gen))
        else:
            await _client().incr(GEN_KEY)
    except Exception as err:
        _failed(err)


async def remember(key: str, ttl: int, compute: Callable[[], Awaitable[Any]]) -> Any:
    """The cached value, or compute() stored for ttl seconds. compute's result must be JSON-safe."""
    hit = await get(key)
    if hit is not None:
        return hit
    value = await compute()
    await put(key, value, ttl)
    return value


def clear_local() -> None:
    """Tests: forget everything held in this process."""
    global _down_until
    _local.clear()
    _down_until = 0.0
