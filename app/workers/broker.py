"""RabbitMQ plumbing shared by the API (publishing) and the worker (consuming).

Every queue in QUEUES gets two companions:
  <name>.retry  failed tasks wait here for `retry_delay_ms`, then dead-letter back to <name>
  <name>.dead   tasks that exhausted their retries, were malformed, or raised PermanentError
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueSpec:
    name: str
    # Fixed when RabbitMQ first declares <name>.retry. Changing it later means
    # deleting that queue in RabbitMQ, otherwise declaration fails.
    retry_delay_ms: int = 30_000


# Add a queue here to isolate a kind of work (so a backlog of one can't delay
# another) and run workers for it with `--queues`.
QUEUES: dict[str, QueueSpec] = {
    spec.name: spec
    for spec in (
        QueueSpec("default"),
        QueueSpec("emails", retry_delay_ms=30_000),
    )
}


def retry_queue_name(queue: str) -> str:
    return f"{queue}.retry"


def dead_queue_name(queue: str) -> str:
    return f"{queue}.dead"


async def declare_queue(channel: AbstractChannel, spec: QueueSpec) -> AbstractQueue:
    await channel.declare_queue(dead_queue_name(spec.name), durable=True)
    await channel.declare_queue(
        retry_queue_name(spec.name),
        durable=True,
        arguments={
            "x-message-ttl": spec.retry_delay_ms,
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": spec.name,
        },
    )
    return await channel.declare_queue(spec.name, durable=True)


def build_message(body: bytes, headers: Optional[dict] = None) -> aio_pika.Message:
    return aio_pika.Message(
        body=body,
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        headers=headers or {},
    )


async def connect() -> AbstractRobustConnection:
    """Connect, retrying until RabbitMQ is up. After that, the robust connection reconnects itself."""
    while True:
        try:
            return await aio_pika.connect_robust(settings.RABBITMQ_URL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"RabbitMQ unavailable ({e}); retrying in 5s")
            await asyncio.sleep(5)


class Publisher:
    """Process-wide publishing connection.

    Connects in the background so the API still boots if RabbitMQ is down;
    `ready` is False until then (see Task.enqueue for what happens meanwhile).
    """

    def __init__(self) -> None:
        self._connection: Optional[AbstractRobustConnection] = None
        self._channel: Optional[AbstractChannel] = None
        self._connect_task: Optional[asyncio.Task] = None

    @property
    def ready(self) -> bool:
        return self._channel is not None and not self._channel.is_closed

    async def start(self) -> None:
        self._connect_task = asyncio.create_task(self._connect())

    async def stop(self) -> None:
        if self._connect_task:
            self._connect_task.cancel()
        if self._connection:
            await self._connection.close()
        self._connection = self._channel = None

    async def publish(self, queue: str, envelope: dict) -> None:
        if not self.ready:
            raise RuntimeError("RabbitMQ publisher is not connected")
        # Publisher confirms are on by default: this returns once the broker has persisted it.
        await self._channel.default_exchange.publish(
            build_message(json.dumps(envelope).encode()), routing_key=queue
        )

    async def _connect(self) -> None:
        self._connection = await connect()
        self._channel = await self._connection.channel()
        for spec in QUEUES.values():
            await declare_queue(self._channel, spec)
        logger.info("Publisher connected to RabbitMQ")


publisher = Publisher()
