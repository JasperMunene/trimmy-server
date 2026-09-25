import asyncio
import json
import logging
import signal
import time
from functools import partial

from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

from app.workers.broker import (
    QUEUES,
    build_message,
    connect,
    dead_queue_name,
    declare_queue,
    publisher,
    retry_queue_name,
)
from app.workers.registry import TASKS, PermanentError, discover_tasks

logger = logging.getLogger("worker")

SHUTDOWN_GRACE_SECONDS = 30


async def process_message(message: AbstractIncomingMessage, channel: AbstractChannel, queue: str) -> None:
    """Run one task message and settle it: ack, or re-route to <queue>.retry / <queue>.dead."""
    attempt = int((message.headers or {}).get("x-attempt", 0))

    try:
        envelope = json.loads(message.body)
        name, task_id, kwargs = envelope["task"], envelope["id"], envelope.get("kwargs", {})
    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"[{queue}] malformed message: {e}")
        await _reroute(channel, message, dead_queue_name(queue), attempt, f"malformed message: {e}")
        return

    task = TASKS.get(name)
    if task is None:
        logger.error(f"[{queue}] unknown task {name} [{task_id}]")
        await _reroute(channel, message, dead_queue_name(queue), attempt, f"unknown task: {name}")
        return

    label = f"{name} [{task_id}] attempt {attempt + 1}"
    started = time.monotonic()
    try:
        await task.run(kwargs)
    except PermanentError as e:
        logger.error(f"{label} failed permanently: {e}")
        await _reroute(channel, message, dead_queue_name(queue), attempt, str(e))
        return
    except Exception as e:
        if attempt < task.max_retries:
            logger.warning(f"{label} failed: {e!r}; retrying")
            await _reroute(channel, message, retry_queue_name(queue), attempt + 1, repr(e))
        else:
            logger.exception(f"{label} failed; giving up after {attempt + 1} attempts")
            await _reroute(channel, message, dead_queue_name(queue), attempt, repr(e))
        return

    logger.info(f"{label} done in {(time.monotonic() - started) * 1000:.0f}ms")
    await message.ack()


async def _reroute(
    channel: AbstractChannel,
    message: AbstractIncomingMessage,
    target: str,
    attempt: int,
    error: str,
) -> None:
    # Publish the copy before acking the original: a crash in between can
    # duplicate a task, but never lose one.
    await channel.default_exchange.publish(
        build_message(message.body, headers={"x-attempt": attempt, "x-error": error[:1000]}),
        routing_key=target,
    )
    await message.ack()


class Worker:
    def __init__(self, queues: list[str], concurrency: int) -> None:
        unknown = set(queues) - QUEUES.keys()
        if unknown:
            raise ValueError(f"Unknown queues: {', '.join(sorted(unknown))}")
        self.queues = queues
        self.concurrency = concurrency
        self._inflight: set[asyncio.Task] = set()

    async def run(self) -> None:
        discover_tasks()

        connection = await connect()
        channel = await connection.channel()
        # prefetch bounds how many messages this worker holds at once; aio-pika
        # runs each in its own asyncio task, so this is also the concurrency.
        await channel.set_qos(prefetch_count=self.concurrency)
        # Tasks may enqueue follow-up tasks.
        await publisher.start()

        consumers = []
        for name in self.queues:
            queue = await declare_queue(channel, QUEUES[name])
            tag = await queue.consume(partial(self._on_message, channel, name))
            consumers.append((queue, tag))

        logger.info(
            f"Worker consuming {', '.join(self.queues)} (concurrency={self.concurrency}); "
            f"tasks: {', '.join(sorted(TASKS))}"
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        logger.info("Shutting down: no new messages, waiting for in-flight tasks")
        for queue, tag in consumers:
            await queue.cancel(tag)
        if self._inflight:
            done, pending = await asyncio.wait(self._inflight, timeout=SHUTDOWN_GRACE_SECONDS)
            if pending:
                # Unacked messages go back to the queue when the channel closes.
                logger.warning(f"{len(pending)} task(s) still running after {SHUTDOWN_GRACE_SECONDS}s; they will be redelivered")
        await publisher.stop()
        await connection.close()

    async def _on_message(self, channel: AbstractChannel, queue: str, message: AbstractIncomingMessage) -> None:
        current = asyncio.current_task()
        self._inflight.add(current)
        try:
            await process_message(message, channel, queue)
        finally:
            self._inflight.discard(current)
