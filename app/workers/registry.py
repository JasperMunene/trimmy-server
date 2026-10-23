"""Task registry.

Tasks live next to the business logic they drive, in app/modules/<module>/tasks.py
(picked up automatically by the worker). Define one:

    # app/modules/business/tasks.py
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.workers.registry import task

    @task("business.recompute_rating", queue="default", max_retries=3)
    async def recompute_rating(business_id: str, db: AsyncSession) -> None:
        from app.modules.business.service import BusinessService
        await BusinessService(db).recompute_rating(business_id)

Declaring a `db: AsyncSession` parameter gets you a fresh session per run
(closed afterwards, rolled back if the task raises), the same way routers get
one from Depends(get_db). Import services inside the task body, since
services import their tasks to enqueue them.

Enqueue it from anywhere (API request, service, another task):

    await recompute_rating.enqueue(business_id=str(business.id))

Periodic tasks run on a schedule. `python worker.py --beat` (exactly one
instance) enqueues them when due; normal workers run them like any task:

    @periodic("booking.expire_holds", every=60)
    async def expire_holds_job(db: AsyncSession) -> None: ...

A periodic task takes no arguments besides an optional `db`, and must be safe
to run twice at once (a slow run can overlap the next).

Tasks are async and take JSON-serialisable keyword arguments. Raising any
exception retries the task (up to max_retries, spaced by the queue's
retry delay); raise PermanentError to skip straight to the dead queue.
Tasks can run more than once (retries, redelivery after a worker crash), so
make them idempotent.
"""
import asyncio
import importlib
import importlib.util
import inspect
import json
import logging
import pkgutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.core.database import AsyncSessionLocal
from app.workers.broker import QUEUES, publisher

logger = logging.getLogger(__name__)


class PermanentError(Exception):
    """Raise from a task when retrying can't help (bad input, record gone, ...)."""


class EnqueueError(RuntimeError):
    pass


@dataclass
class Task:
    name: str
    fn: Callable[..., Awaitable[Any]]
    queue: str
    max_retries: int
    inline_fallback: bool

    @property
    def wants_db(self) -> bool:
        return "db" in inspect.signature(self.fn).parameters

    async def run(self, kwargs: dict) -> None:
        """Execute the task body, injecting a DB session if it asks for one."""
        if not self.wants_db:
            await self.fn(**kwargs)
            return
        async with AsyncSessionLocal() as db:
            try:
                await self.fn(**kwargs, db=db)
            except BaseException:
                await db.rollback()
                raise

    async def enqueue(self, **kwargs: Any) -> str:
        if "db" in kwargs:
            raise TypeError(f"{self.name}: `db` is injected by the worker; don't pass it")
        # Round-trip through JSON so inline runs see exactly what a worker would.
        kwargs = json.loads(json.dumps(kwargs))
        envelope = {
            "id": uuid.uuid4().hex,
            "task": self.name,
            "kwargs": kwargs,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }

        if publisher.ready:
            try:
                await publisher.publish(self.queue, envelope)
                return envelope["id"]
            except Exception as e:
                logger.warning(f"Failed to enqueue {self.name}: {e}")

        if not self.inline_fallback:
            raise EnqueueError(f"Could not enqueue {self.name}: RabbitMQ unavailable")

        logger.warning(f"RabbitMQ unavailable; running {self.name} [{envelope['id']}] in-process")
        _spawn(self._run_inline(envelope["id"], kwargs))
        return envelope["id"]

    async def _run_inline(self, task_id: str, kwargs: dict) -> None:
        try:
            await self.run(kwargs)
        except Exception:
            logger.exception(f"Inline run of {self.name} [{task_id}] failed")


TASKS: dict[str, Task] = {}


@dataclass(frozen=True)
class Periodic:
    task: Task
    every_seconds: int


PERIODIC: dict[str, Periodic] = {}

# Strong references so in-process fallback runs aren't garbage-collected mid-flight.
_background: set[asyncio.Task] = set()


def _spawn(coro: Awaitable[Any]) -> None:
    t = asyncio.create_task(coro)
    _background.add(t)
    t.add_done_callback(_background.discard)


def task(
    name: str,
    *,
    queue: str = "default",
    max_retries: int = 5,
    inline_fallback: bool = False,
) -> Callable[[Callable[..., Awaitable[Any]]], Task]:
    """Register an async function as a background task.

    inline_fallback: if RabbitMQ is unreachable, run the task in the calling
    process instead of raising EnqueueError. Use it for work that must not be
    dropped and is cheap enough to run in the API (e.g. OTP emails).
    """
    if queue not in QUEUES:
        raise ValueError(f"Unknown queue {queue!r}; add it to QUEUES in app/workers/broker.py")

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Task:
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(f"Task {name} must be an async function")
        if name in TASKS:
            raise ValueError(f"Task {name} is already registered")
        TASKS[name] = Task(name, fn, queue, max_retries, inline_fallback)
        return TASKS[name]

    return decorator


def periodic(
    name: str, *, every: int, queue: str = "default", max_retries: int = 0
) -> Callable[[Callable[..., Awaitable[Any]]], Task]:
    """Register a task that beat enqueues every `every` seconds.

    No retries by default: the next scheduled run is the retry.
    """
    if every < 1:
        raise ValueError("every must be at least 1 second")

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Task:
        params = set(inspect.signature(fn).parameters) - {"db"}
        if params:
            raise TypeError(f"Periodic task {name} can't take arguments besides db (got {', '.join(sorted(params))})")
        registered = task(name, queue=queue, max_retries=max_retries)(fn)
        PERIODIC[name] = Periodic(registered, every)
        return registered

    return decorator


def discover_tasks() -> None:
    """Import every app/modules/<module>/tasks.py so its tasks register.

    Also imports every module's models first, so SQLAlchemy can resolve
    relationships and foreign keys between modules inside the worker.
    """
    import app.modules as modules_pkg

    packages = [info.name for info in pkgutil.iter_modules(modules_pkg.__path__) if info.ispkg]
    for sub in ("models", "tasks"):
        for package in packages:
            name = f"{modules_pkg.__name__}.{package}.{sub}"
            if importlib.util.find_spec(name) is not None:
                importlib.import_module(name)
