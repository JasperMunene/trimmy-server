import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.modules.auth.sms as sms
import app.modules.booking.tasks as booking_tasks
import app.workers.registry as registry
from app.modules.booking.models import Booking
from app.modules.booking.tasks import due_reminder, reminder_sms
from app.workers.registry import TASKS, discover_tasks
from tests.test_create_booking import customer, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import insert_booking

NBO = ZoneInfo("Africa/Nairobi")
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


# ─── Which reminder is due ───────────────────────────────────


@pytest.mark.parametrize("left,booked_ahead,sent,expected", [
    (30 * H, 3 * 24 * H, (False, False), None),     # too early
    (24 * H, 3 * 24 * H, (False, False), "24h"),    # exactly a day before
    (20 * H, 3 * 24 * H, (False, False), "24h"),    # beat was a little late
    (20 * H, 3 * 24 * H, (True, False), None),      # already sent
    (20 * H, 10 * H, (False, False), None),         # booked the same day: the confirmation is enough
    (2 * H, 3 * 24 * H, (False, False), "2h"),      # 2h window: the 24h one is skipped
    (90 * timedelta(minutes=1), 3 * 24 * H, (True, False), "2h"),
    (90 * timedelta(minutes=1), 3 * 24 * H, (True, True), None),
    (1 * H, 100 * timedelta(minutes=1), (False, False), None),  # booked 40 minutes ago, for 1h from now
    (timedelta(0), 3 * 24 * H, (False, False), None),           # starting now
])
def test_due_reminder(left, booked_ahead, sent, expected):
    starts = NOW + left
    assert due_reminder(starts, starts - booked_ahead, NOW, *sent) == expected


# ─── Messages ────────────────────────────────────────────────


def test_day_before_message():
    when = datetime(2026, 10, 5, 14, 30, tzinfo=NBO)
    msg = reminder_sms("24h", reference="BK7F3AQ9", business="Glow Beauty", services=["Classic Haircut"], staff="Wanjiku",
                       when=when, now=datetime(2026, 10, 4, 14, 30, tzinfo=NBO))
    assert msg == "Trimmy reminder: Glow Beauty, Classic Haircut with Wanjiku, tomorrow 14:30. Ref BK7F3AQ9. To change it, open the Trimmy app."
    assert len(msg) <= 160


def test_day_before_message_across_midnight_says_today():
    when = datetime(2026, 10, 5, 9, 0, tzinfo=NBO)
    msg = reminder_sms("24h", reference="BK1", business="Glow", services=[], staff=None, when=when,
                       now=datetime(2026, 10, 5, 7, 0, tzinfo=NBO) - timedelta(hours=1))
    assert "today 09:00" in msg and "your appointment" in msg


def test_two_hour_message_has_directions():
    when = datetime(2026, 10, 5, 14, 30, tzinfo=NBO)
    msg = reminder_sms("2h", reference="BK7F3AQ9", business="Glow Beauty", services=["Cut", "Colour"], staff=None, when=when,
                       now=when - 2 * H, location={"address": "Kilimani", "lat": -1.29, "lng": 36.78})
    assert msg == "Trimmy: see you at 14:30 at Glow Beauty (Cut +1 more). Ref BK7F3AQ9. Directions: https://maps.google.com/?q=-1.29,36.78"
    no_coords = reminder_sms("2h", reference="BK1", business="Glow", services=["Cut"], staff=None, when=when, now=when,
                             location={"address": "Yaya Centre"})
    assert no_coords.endswith("Ref BK1. Yaya Centre.")


# ─── Scan and send ───────────────────────────────────────────


@pytest.fixture
def outbox(monkeypatch, db_session):
    discover_tasks()
    monkeypatch.setattr(registry, "AsyncSessionLocal", async_sessionmaker(bind=db_session.bind))
    sent = []
    monkeypatch.setattr(sms, "deliver_sms", lambda to, message, log_hint="": sent.append((to, message)))

    # Run queued sends straight away (tests have no RabbitMQ).
    async def enqueue_now(**kwargs):
        await TASKS["booking.send_reminder"].run(kwargs)

    monkeypatch.setattr(booking_tasks.send_booking_reminder, "enqueue", enqueue_now)
    return sent


async def scan():
    await TASKS["booking.scan_reminders"].run({})


async def upcoming(db_session, salon, user_id, starts_in, booked_ago=timedelta(days=3), **extra):
    now = datetime.now(timezone.utc)
    b = await insert_booking(db_session, salon, user_id, now + starts_in, customer_phone="+254722000111", **extra)
    b.created_at = now - booked_ago
    await db_session.commit()
    return b.id


@pytest.mark.asyncio
async def test_scan_sends_each_reminder_once(salon, customer, db_session, outbox):
    user, _ = customer
    tomorrow = await upcoming(db_session, salon, user.id, 23 * H)
    soon = await upcoming(db_session, salon, user.id, 90 * timedelta(minutes=1))
    await upcoming(db_session, salon, user.id, 5 * 24 * H)  # not yet
    await scan()
    await scan()  # the next run doesn't repeat them

    assert len(outbox) == 2
    assert any("Trimmy reminder:" in m for _, m in outbox) and any("see you at" in m for _, m in outbox)
    db_session.expire_all()
    assert (await db_session.get(Booking, tomorrow)).reminder_24h_sent_at is not None
    assert (await db_session.get(Booking, soon)).reminder_2h_sent_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"status": "cancelled"},
    {"status": "pending_payment", "deposit_amount": 200},
    {"customer_phone": None},
])
async def test_no_reminders_for_these(salon, customer, db_session, outbox, extra):
    user, _ = customer
    now = datetime.now(timezone.utc)
    b = await insert_booking(db_session, salon, user.id, now + 23 * H, **extra)
    b.created_at = now - timedelta(days=3)
    if extra.get("status") == "pending_payment":
        b.expires_at = now + timedelta(minutes=5)
    await db_session.commit()
    await scan()
    assert outbox == []


@pytest.mark.asyncio
async def test_rescheduling_resets_reminders(client, salon, customer, db_session, outbox):
    user, cust = customer
    booking_id = await upcoming(db_session, salon, user.id, 23 * H)
    await scan()
    assert len(outbox) == 1
    # Owner moves it (customers can't: it's inside the 24h free window).
    from app.modules.booking.reschedule import RescheduleService
    from app.modules.booking.schemas import RescheduleRequest
    from app.modules.business.models import Business
    owner = await db_session.get(type(user), (await db_session.get(Business, uuid.UUID(salon["id"]))).owner_id)
    new_day = (datetime.now(NBO) + timedelta(days=2)).date()
    await RescheduleService(db_session).reschedule(owner, str(booking_id), RescheduleRequest(date=new_day, time="10:00"))
    db_session.expire_all()
    moved = await db_session.get(Booking, booking_id)
    assert (moved.reminder_24h_sent_at, moved.reminder_2h_sent_at) == (None, None)


@pytest.mark.asyncio
async def test_failed_send_is_released_for_a_retry(salon, customer, db_session, outbox, monkeypatch):
    user, _ = customer
    booking_id = await upcoming(db_session, salon, user.id, 23 * H)

    def down(to, message, log_hint=""):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(sms, "deliver_sms", down)
    with pytest.raises(RuntimeError):
        await TASKS["booking.send_reminder"].run({"booking_id": str(booking_id), "kind": "24h"})
    db_session.expire_all()
    assert (await db_session.get(Booking, booking_id)).reminder_24h_sent_at is None


# ─── Preferences ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preferences_default_on_and_patch_only_changes_what_is_sent(client, customer):
    _, h = customer
    url = "/api/v1/me/notification-preferences"
    everything = {"dayBefore": True, "twoHoursBefore": True, "sms": True, "push": True}
    assert (await client.get(url, headers=h)).json() == {"reminders": everything}
    resp = await client.patch(url, json={"reminders": {"dayBefore": False}}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reminders"] == {**everything, "dayBefore": False}
    again = await client.patch(url, json={"reminders": {"sms": False}}, headers=h)
    assert again.json()["reminders"] == {**everything, "dayBefore": False, "sms": False}
    assert (await client.get(url)).status_code == 401


@pytest.fixture
def pushed(monkeypatch):
    calls = []

    async def record(booking_id, kind):
        calls.append(kind)

    monkeypatch.setattr(booking_tasks, "push_update", record)
    return calls


@pytest.mark.asyncio
async def test_turned_off_reminder_is_not_sent(client, salon, customer, db_session, outbox, pushed):
    user, h = customer
    await client.patch("/api/v1/me/notification-preferences", json={"reminders": {"dayBefore": False}}, headers=h)
    booking_id = await upcoming(db_session, salon, user.id, 23 * H)
    await scan()
    assert (outbox, pushed) == ([], [])
    db_session.expire_all()
    assert (await db_session.get(Booking, booking_id)).reminder_24h_sent_at is not None  # not retried


@pytest.mark.asyncio
async def test_channels(client, salon, customer, db_session, outbox, pushed):
    user, h = customer
    url = "/api/v1/me/notification-preferences"
    await client.patch(url, json={"reminders": {"sms": False}}, headers=h)
    await upcoming(db_session, salon, user.id, 23 * H)
    await scan()
    assert (outbox, pushed) == ([], ["reminder_24h"])

    await client.patch(url, json={"reminders": {"sms": True, "push": False}}, headers=h)
    await upcoming(db_session, salon, user.id, 90 * timedelta(minutes=1))
    await scan()
    assert len(outbox) == 1 and pushed == ["reminder_24h"]
