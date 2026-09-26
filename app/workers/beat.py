"""Beat: enqueues periodic tasks when they're due (run exactly one: `python worker.py --beat`).

Beat never runs task code itself; it publishes to RabbitMQ and the normal
workers pick the jobs up, with the usual retries and dead-lettering. Stopping
beat only stops new periodic runs.
"""
import asyncio
import logging
import signal
import time
from typing import Callable, Optional

from app.workers.broker import publisher
from app.workers.registry import PERIODIC, EnqueueError, Periodic, discover_tasks

logger = logging.getLogger("beat")


class Schedule:
    """When each periodic task is next due. Pure: the clock is passed in."""

    def __init__(self, jobs: list[Periodic], now: float):
        # Everything runs once shortly after start, then on its interval.
        self.next_run = {job.task.name: now for job in jobs}
        self.jobs = {job.task.name: job for job in jobs}

    def due(self, now: float) -> list[Periodic]:
        """The jobs due at `now`, scheduling each one's next run.

        After a pause (beat was down, or a slow tick) a job runs once, not once
        per missed interval.
        """
        ready = []
        for name, at in self.next_run.items():
            if now >= at:
                job = self.jobs[name]
                ready.append(job)
                on_time = at + job.every_seconds
                # Late (a pause): restart the interval from now rather than firing again soon.
                self.next_run[name] = on_time if on_time > now else now + job.every_seconds
        return ready

    def seconds_until_next(self, now: float) -> float:
        return max(0.0, min(self.next_run.values(), default=now + 60) - now)


class Beat:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._stop = asyncio.Event()

    async def run(self) -> None:
        discover_tasks()
        jobs = list(PERIODIC.values())
        if not jobs:
            logger.warning("No periodic tasks registered; beat has nothing to do")
        await publisher.start()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        schedule = Schedule(jobs, self.clock())
        logger.info("Beat scheduling: " + ", ".join(f"{j.task.name} every {j.every_seconds}s" for j in jobs))
        while not self._stop.is_set():
            for job in schedule.due(self.clock()):
                await self.enqueue(job)
            await self._sleep(min(schedule.seconds_until_next(self.clock()), 5.0))

        logger.info("Beat stopping")
        await publisher.stop()

    async def enqueue(self, job: Periodic) -> Optional[str]:
        try:
            task_id = await job.task.enqueue()
            logger.info(f"Enqueued {job.task.name} [{task_id}]")
            return task_id
        except EnqueueError as e:
            # RabbitMQ is down; the next due time tries again.
            logger.warning(f"Could not enqueue {job.task.name}: {e}")
            return None

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
