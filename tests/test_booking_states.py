import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.modules.booking.models import Booking, BookingLine, new_reference
from app.modules.booking.states import TRANSITIONS, InvalidTransition, can_transition, record_created, transition
from app.modules.business.models import Business, Staff

STATUSES = ["pending_payment", "confirmed", "in_progress", "completed", "no_show", "cancelled"]
ALLOWED = {
    ("pending_payment", "confirmed"), ("pending_payment", "cancelled"),
    ("confirmed", "in_progress"), ("confirmed", "no_show"), ("confirmed", "cancelled"),
    ("in_progress", "completed"),
}
NBO = ZoneInfo("Africa/Nairobi")
on_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"), reason="needs Postgres (BK-05)"
)


def make_booking(**fields) -> Booking:
    start = fields.pop("starts_at", datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc))
    base = dict(
        business_id=uuid.uuid4(), starts_at=start, ends_at=start + timedelta(minutes=45),
        local_date=start.astimezone(NBO).date(), local_time=start.astimezone(NBO).strftime("%H:%M"),
        duration_minutes=45, total_amount=800, customer_name="Amina",
    )
    base.update(fields)
    return Booking(**base)


# ─── Transition table ────────────────────────────────────────


@pytest.mark.parametrize("from_status", STATUSES)
@pytest.mark.parametrize("to_status", STATUSES)
def test_transition_table(from_status, to_status):
    assert can_transition(from_status, to_status) == ((from_status, to_status) in ALLOWED)


def test_every_status_is_covered():
    assert set(TRANSITIONS) == set(STATUSES)


def test_record_created_logs_the_first_status():
    b = make_booking()
    actor = uuid.uuid4()
    record_created(b, "pending_payment", actor_id=actor)
    assert b.status == "pending_payment"
    (event,) = b.events
    assert (event.from_status, event.to_status, event.actor_id) == (None, "pending_payment", actor)


@pytest.mark.parametrize("status", ["completed", "no_show", "cancelled"])
def test_bookings_cannot_start_finished(status):
    with pytest.raises(InvalidTransition):
        record_created(make_booking(), status)


def test_cancel_records_reason_and_releases_hold():
    b = make_booking(expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    record_created(b, "pending_payment")
    transition(b, "cancelled", reason="payment_expired")
    assert (b.status, b.cancel_reason, b.expires_at) == ("cancelled", "payment_expired", None)
    assert [(e.from_status, e.to_status, e.reason) for e in b.events] == [
        (None, "pending_payment", None), ("pending_payment", "cancelled", "payment_expired"),
    ]


def test_invalid_transition_changes_nothing():
    b = make_booking()
    record_created(b, "confirmed")
    with pytest.raises(InvalidTransition) as e:
        transition(b, "completed")
    assert "confirmed" in str(e.value) and "completed" in str(e.value)
    assert b.status == "confirmed" and len(b.events) == 1


def test_reference_format():
    refs = {new_reference() for _ in range(200)}
    assert all(re.fullmatch(r"BK[A-HJ-NP-Z2-9]{6}", r) for r in refs)
    assert len(refs) > 190  # effectively unique
    assert all(len(r) <= 12 for r in refs)  # fits M-Pesa's AccountReference


# ─── Persistence ─────────────────────────────────────────────


@pytest.fixture
async def published(db_session, test_user):
    business = Business(owner_id=test_user.id, name="Glow", phone="+254700881122", status="published",
                        location={"address": "Kilimani", "lat": -1.29, "lng": 36.78})
    db_session.add(business)
    await db_session.flush()
    staff = Staff(id=uuid.uuid4(), business_id=business.id, name="Wanjiku", role="staff")
    db_session.add(staff)
    await db_session.commit()
    return business, staff


@pytest.mark.asyncio
async def test_booking_with_lines_and_events_round_trips(db_session, published, test_user):
    business, staff = published
    b = make_booking(business_id=business.id, staff_id=staff.id, customer_id=test_user.id)
    b.lines.append(BookingLine(name="Cut", duration_minutes=45, price_amount=800, position=0))
    record_created(b, "confirmed", actor_id=test_user.id)
    db_session.add(b)
    await db_session.commit()
    booking_id = b.id

    db_session.expire_all()
    loaded = (
        await db_session.execute(
            select(Booking).where(Booking.id == booking_id).options(selectinload(Booking.lines), selectinload(Booking.events))
        )
    ).scalar_one()
    assert loaded.reference.startswith("BK")
    assert (loaded.status, loaded.payment_status, loaded.source, loaded.currency) == ("confirmed", "unpaid", "online", "KES")
    assert [line.name for line in loaded.lines] == ["Cut"]
    assert [e.to_status for e in loaded.events] == ["confirmed"]


@pytest.mark.asyncio
async def test_availability_skips_booked_and_honours_expired_holds(client, db_session, published):
    business, staff = published
    from app.modules.business.models import Service, ServiceCategory, WorkingHours

    cat = ServiceCategory(business_id=business.id, name="Hair")
    db_session.add(cat)
    await db_session.flush()
    svc = Service(business_id=business.id, category_id=cat.id, name="Cut", duration_minutes=60, price_amount=800)
    db_session.add(svc)
    db_session.add(WorkingHours(business_id=business.id, weekly_hours={
        d: {"open": "09:00", "close": "17:00"} for d in
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    }))
    day = (datetime.now(NBO) + timedelta(days=7)).date()
    ten = datetime(day.year, day.month, day.day, 10, 0, tzinfo=NBO)
    noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=NBO)
    confirmed = make_booking(business_id=business.id, staff_id=staff.id, starts_at=ten.astimezone(timezone.utc))
    record_created(confirmed, "confirmed")
    stale_hold = make_booking(
        business_id=business.id, staff_id=staff.id, starts_at=noon.astimezone(timezone.utc),
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    record_created(stale_hold, "pending_payment")
    db_session.add_all([confirmed, stale_hold])
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/businesses/{business.id}/availability",
        params={"date": day.isoformat(), "serviceIds": str(svc.id), "staffId": str(staff.id)},
    )
    times = [s["time"] for s in resp.json()["slots"]]
    assert "10:00" not in times and "09:45" not in times  # 60-minute cut would overlap 10:00-10:45
    assert "09:00" in times and "10:45" in times
    assert "12:00" in times  # the expired hold no longer blocks


# ─── Postgres exclusion constraint (runs once BK-05 adds Postgres to CI) ─


@on_postgres
@pytest.mark.asyncio
async def test_database_rejects_overlapping_assigned_bookings(db_session, published):
    business, staff = published
    first = make_booking(business_id=business.id, staff_id=staff.id)
    record_created(first, "confirmed")
    db_session.add(first)
    await db_session.commit()

    clash = make_booking(business_id=business.id, staff_id=staff.id, starts_at=first.starts_at + timedelta(minutes=15))
    record_created(clash, "confirmed")
    db_session.add(clash)
    with pytest.raises(IntegrityError):
        await db_session.commit()
