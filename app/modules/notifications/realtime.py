"""Live events to signed-in apps over server-sent events (GET /me/events).

Any process (API or worker) calls `publish(user_id, ...)`. With REALTIME_BACKEND=redis that is a
Redis PUBLISH on `rt:u:<user_id>`; every API process holds ONE pattern subscription (`rt:u:*`) and
fans each message out in memory to that user's open streams, so open streams cost no Redis or
database connections. With `local` (tests, or one process without Redis) publishing goes straight
to this process's streams.

Events are best-effort: a stream that misses one (Redis down, a slow client) catches up from the
inbox, which is the source of truth (see router.py).
"""
import asyncio
import json
import logging
import time
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "rt:u:"
QUEUE_SIZE = 100  # per stream; a client this far behind is told to resync instead
RESYNC = {"event": "resync", "data": {}}

_redis = None
_redis_down_until = 0.0  # skip Redis for a while after a failure instead of waiting on it every time


def _client():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=2,
                                   health_check_interval=30)
    return _redis


def _uses_redis() -> bool:
    return settings.REALTIME_BACKEND == "redis" and bool(settings.REDIS_URL)


async def publish(user_id: Any, event: str, data: dict, event_id: Optional[str] = None) -> None:
    """Send one event to every open stream of one user. Never raises."""
    global _redis_down_until
    message = {"event": event, "data": data, "id": event_id}
    if _uses_redis() and time.monotonic() >= _redis_down_until:
        try:
            await _client().publish(f"{CHANNEL_PREFIX}{user_id}", json.dumps(message, default=str))
            return
        except Exception as e:
            _redis_down_until = time.monotonic() + 15
            logger.warning(f"Realtime publish via Redis failed, delivering in-process only: {e}")
    hub.dispatch(str(user_id), message)


class Hub:
    """This process's open streams, by user."""

    def __init__(self) -> None:
        self._streams: dict[str, set[asyncio.Queue]] = {}
        self._task: Optional[asyncio.Task] = None
        self._redis_live = False

    @property
    def healthy(self) -> bool:
        """False while Redis-published events can't reach this process; streams then poll the inbox."""
        return not _uses_redis() or self._redis_live

    def open(self, user_id: str) -> Optional[asyncio.Queue]:
        """A queue of events for a new stream, or None if this user already has too many open."""
        streams = self._streams.setdefault(user_id, set())
        if len(streams) >= settings.SSE_MAX_STREAMS_PER_USER:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        streams.add(queue)
        if _uses_redis() and self._task is None:
            self._task = asyncio.create_task(self._listen())
        return queue

    def close(self, user_id: str, queue: asyncio.Queue) -> None:
        streams = self._streams.get(user_id)
        if streams is not None:
            streams.discard(queue)
            if not streams:
                del self._streams[user_id]

    def dispatch(self, user_id: str, message: dict) -> None:
        for queue in self._streams.get(user_id, ()):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Too far behind: drop what's queued and have the app refetch instead.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(RESYNC)

    def stream_count(self, user_id: str) -> int:
        return len(self._streams.get(user_id, ()))

    @property
    def open_streams(self) -> int:
        return sum(len(s) for s in self._streams.values())

    async def _listen(self) -> None:
        """One pattern subscription for the whole process, reconnecting with backoff."""
        delay = 1.0
        while True:
            pubsub = None
            try:
                pubsub = _client().pubsub(ignore_subscribe_messages=True)
                await pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
                self._redis_live, delay = True, 1.0
                logger.info("Realtime: subscribed to Redis")
                while True:
                    msg = await pubsub.get_message(timeout=30)
                    if msg is None or msg.get("type") != "pmessage":
                        continue
                    channel = msg["channel"].decode() if isinstance(msg["channel"], bytes) else msg["channel"]
                    try:
                        payload = json.loads(msg["data"])
                    except (TypeError, ValueError):
                        continue
                    self.dispatch(channel[len(CHANNEL_PREFIX):], payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._redis_live = False
                logger.warning(f"Realtime: Redis subscription lost ({e}); retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._redis_live = False


hub = Hub()


def sse(event: str, data: Any, event_id: Optional[str] = None) -> str:
    """One server-sent event. `data` is JSON on a single line, as the app's parser expects."""
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"
