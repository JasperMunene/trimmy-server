import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.workers.registry as registry
from app.workers.beat import Beat, Schedule
from app.workers.broker import publisher
from app.workers.registry import PERIODIC, TASKS, Periodic, periodic, task


@pytest.fixture
def scratch():
    """Register throwaway (periodic) tasks and remove them afterwards."""
    before_tasks, before_periodic = set(TASKS), set(PERIODIC)
    yield
    for name in set(TASKS) - before_tasks:
        del TASKS[name]
    for name in set(PERIODIC) - before_periodic:
        del PERIODIC[name]


def job(name, every):
    @task(name)
    async def fn() -> None:
        pass

    return Periodic(fn, every)


def names(jobs):
    return sorted(j.task.name for j in jobs)


def test_everything_runs_right_after_start(scratch):
    schedule = Schedule([job("t.a", 60), job("t.b", 300)], now=1000)
    assert names(schedule.due(1000)) == ["t.a", "t.b"]
    assert schedule.due(1000) == []


def test_runs_on_its_interval(scratch):
    schedule = Schedule([job("t.a", 60), job("t.b", 300)], now=0)
    schedule.due(0)
    assert names(schedule.due(59)) == []
    assert names(schedule.due(60)) == ["t.a"]
    assert names(schedule.due(120)) == ["t.a"]
    assert names(schedule.due(300)) == ["t.a", "t.b"]


def test_no_burst_after_a_pause(scratch):
    schedule = Schedule([job("t.a", 60)], now=0)
    schedule.due(0)
    # Beat was stuck for ten intervals: one run, not ten.
    assert names(schedule.due(600)) == ["t.a"]
    assert schedule.due(601) == []
    assert names(schedule.due(660)) == ["t.a"]


def test_seconds_until_next(scratch):
    schedule = Schedule([job("t.a", 60)], now=0)
    schedule.due(0)
    assert schedule.seconds_until_next(10) == 50


def test_periodic_registers_a_task_and_schedule(scratch):
    @periodic("t.sweep", every=30)
    async def sweep(db: AsyncSession) -> None:
        pass

    assert TASKS["t.sweep"] is sweep
    assert PERIODIC["t.sweep"].every_seconds == 30
    assert sweep.max_retries == 0  # the next run is the retry


def test_periodic_rejects_arguments_and_bad_interval(scratch):
    with pytest.raises(TypeError):
        @periodic("t.bad", every=60)
        async def bad(booking_id: str) -> None:
            pass

    with pytest.raises(ValueError):
        periodic("t.zero", every=0)


@pytest.mark.asyncio
async def test_beat_enqueues_and_survives_a_broker_outage(scratch, monkeypatch):
    j = job("t.a", 60)
    beat = Beat()

    assert not publisher.ready  # no RabbitMQ in tests
    assert await beat.enqueue(j) is None  # logged, not raised

    sent = []

    async def fake_publish(queue, envelope):
        sent.append((queue, envelope["task"]))

    monkeypatch.setattr(type(publisher), "ready", property(lambda self: True))
    monkeypatch.setattr(publisher, "publish", fake_publish)
    assert await beat.enqueue(j) is not None
    assert sent == [("default", "t.a")]
