import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

import app.workers.registry as registry
from app.modules.booking.models import Booking
from app.modules.booking.states import record_created
from app.workers.registry import PERIODIC, TASKS, discover_tasks


def pending(expires_in_minutes, **fields):
    start = datetime.now(timezone.utc) + timedelta(days=2)
    b = Booking(
        business_id=uuid.uuid4(), starts_at=start, ends_at=start + timedelta(minutes=45),
        local_date=start.date(), local_time="10:00", duration_minutes=45, total_amount=800,
        customer_name="Amina", deposit_amount=200,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes), **fields,
    )
    record_created(b, "pending_payment")
    return b


def test_registered_every_minute():
    discover_tasks()
    assert PERIODIC["booking.expire_holds"].every_seconds == 60
    assert TASKS["booking.expire_holds"].queue == "default"


@pytest.mark.asyncio
async def test_sweep_releases_only_expired_holds(db_session, monkeypatch):
    discover_tasks()
    expired, live = pending(-1), pending(+5)
    confirmed = pending(-1)
    confirmed.status = "confirmed"  # holds only apply to pending_payment
    db_session.add_all([expired, live, confirmed])
    await db_session.commit()
    ids = (expired.id, live.id, confirmed.id)

    monkeypatch.setattr(registry, "AsyncSessionLocal", async_sessionmaker(bind=db_session.bind))
    await TASKS["booking.expire_holds"].run({})
    await TASKS["booking.expire_holds"].run({})  # idempotent

    db_session.expire_all()
    rows = {
        b.id: b for b in (await db_session.execute(
            select(Booking).where(Booking.id.in_(ids)).options(selectinload(Booking.events))
        )).scalars()
    }
    assert (rows[ids[0]].status, rows[ids[0]].cancel_reason) == ("cancelled", "payment_expired")
    assert [e.to_status for e in rows[ids[0]].events] == ["pending_payment", "cancelled"]
    assert rows[ids[1]].status == "pending_payment"
    assert rows[ids[2]].status == "confirmed"
