import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.workers.registry as registry
from app.modules.auth.models import User
from app.modules.auth.tasks import send_otp_email
from app.workers.broker import QUEUES, publisher
from app.workers.registry import TASKS, EnqueueError, PermanentError, discover_tasks, task
from app.workers.runner import process_message


class FakeExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key):
        self.published.append((routing_key, message))


class FakeChannel:
    def __init__(self):
        self.default_exchange = FakeExchange()


class FakeMessage:
    def __init__(self, envelope, headers=None):
        self.body = envelope if isinstance(envelope, bytes) else json.dumps(envelope).encode()
        self.headers = headers or {}
        self.acked = False

    async def ack(self):
        self.acked = True


@pytest.fixture
def scratch_tasks():
    """Register throwaway tasks for a test and unregister them afterwards."""
    before = set(TASKS)
    yield
    for name in set(TASKS) - before:
        del TASKS[name]


def envelope(name, **kwargs):
    return {"id": "t1", "task": name, "kwargs": kwargs, "enqueued_at": "2026-09-25T00:00:00Z"}


@pytest.mark.asyncio
async def test_success_acks(scratch_tasks):
    calls = []

    @task("test.ok")
    async def ok(x: int) -> None:
        calls.append(x)

    channel, message = FakeChannel(), FakeMessage(envelope("test.ok", x=1))
    await process_message(message, channel, "default")
    assert calls == [1]
    assert message.acked
    assert channel.default_exchange.published == []


@pytest.mark.asyncio
async def test_failure_goes_to_retry_queue_with_attempt(scratch_tasks):
    @task("test.flaky", max_retries=2)
    async def flaky() -> None:
        raise RuntimeError("boom")

    channel, message = FakeChannel(), FakeMessage(envelope("test.flaky"), headers={"x-attempt": 1})
    await process_message(message, channel, "default")
    (target, copy), = channel.default_exchange.published
    assert target == "default.retry"
    assert copy.headers["x-attempt"] == 2
    assert "boom" in copy.headers["x-error"]
    assert copy.body == message.body
    assert message.acked


@pytest.mark.asyncio
async def test_exhausted_retries_go_to_dead_queue(scratch_tasks):
    @task("test.broken", max_retries=2)
    async def broken() -> None:
        raise RuntimeError("boom")

    channel, message = FakeChannel(), FakeMessage(envelope("test.broken"), headers={"x-attempt": 2})
    await process_message(message, channel, "default")
    assert channel.default_exchange.published[0][0] == "default.dead"


@pytest.mark.asyncio
async def test_permanent_error_skips_retries(scratch_tasks):
    @task("test.permanent")
    async def permanent() -> None:
        raise PermanentError("bad input")

    channel, message = FakeChannel(), FakeMessage(envelope("test.permanent"))
    await process_message(message, channel, "default")
    assert channel.default_exchange.published[0][0] == "default.dead"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json", json.dumps({"no": "task"}).encode()])
async def test_malformed_message_goes_to_dead_queue(body):
    channel, message = FakeChannel(), FakeMessage(body)
    await process_message(message, channel, "emails")
    assert channel.default_exchange.published[0][0] == "emails.dead"
    assert message.acked


@pytest.mark.asyncio
async def test_unknown_task_goes_to_dead_queue():
    channel, message = FakeChannel(), FakeMessage(envelope("test.does_not_exist"))
    await process_message(message, channel, "default")
    assert channel.default_exchange.published[0][0] == "default.dead"


@pytest.mark.asyncio
async def test_db_session_is_injected(scratch_tasks, test_user, db_session, monkeypatch):
    monkeypatch.setattr(registry, "AsyncSessionLocal", async_sessionmaker(bind=db_session.bind))
    seen = []

    @task("test.uses_db")
    async def uses_db(user_id: str, db: AsyncSession) -> None:
        result = await db.execute(select(User.email).where(User.id == user_id))
        seen.append(result.scalar_one())

    await process_message(FakeMessage(envelope("test.uses_db", user_id=str(test_user.id))), FakeChannel(), "default")
    assert seen == ["test@example.com"]


@pytest.mark.asyncio
async def test_enqueue_publishes_envelope(scratch_tasks, monkeypatch):
    @task("test.publish", queue="emails")
    async def to_publish(x: int) -> None:
        pass

    sent = []

    async def fake_publish(queue, env):
        sent.append((queue, env))

    monkeypatch.setattr(type(publisher), "ready", property(lambda self: True))
    monkeypatch.setattr(publisher, "publish", fake_publish)
    task_id = await to_publish.enqueue(x=5)

    (queue, env), = sent
    assert queue == "emails"
    assert env["task"] == "test.publish"
    assert env["kwargs"] == {"x": 5}
    assert env["id"] == task_id


@pytest.mark.asyncio
async def test_enqueue_without_broker_raises_unless_inline_fallback(scratch_tasks):
    @task("test.no_fallback")
    async def no_fallback() -> None:
        pass

    assert not publisher.ready
    with pytest.raises(EnqueueError):
        await no_fallback.enqueue()


@pytest.mark.asyncio
async def test_enqueue_rejects_db_kwarg(scratch_tasks):
    @task("test.db_kwarg")
    async def db_kwarg(db: AsyncSession) -> None:
        pass

    with pytest.raises(TypeError):
        await db_kwarg.enqueue(db="nope")


def test_task_validation(scratch_tasks):
    with pytest.raises(ValueError):
        task("test.bad_queue", queue="nope")
    with pytest.raises(TypeError):
        @task("test.sync")
        def sync() -> None:
            pass


def test_discover_tasks_registers_module_tasks():
    discover_tasks()
    assert TASKS["email.send_otp"] is send_otp_email
    assert send_otp_email.queue in QUEUES


@pytest.mark.asyncio
async def test_send_otp_email_unknown_kind_is_permanent():
    with pytest.raises(PermanentError):
        await send_otp_email.run({"kind": "nope", "to": "a@example.com", "otp": "123456"})
