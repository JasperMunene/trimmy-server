import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.modules.auth.sms as sms
import app.modules.booking.tasks as booking_tasks
import app.workers.registry as registry
from app.modules.booking.models import Booking
from app.modules.booking.tasks import confirmation_sms
from app.workers.registry import TASKS, discover_tasks
from tests.test_create_booking import NBO, body, book, customer, salon, set_deposit  # noqa: F401  (fixtures)
from tests.test_mpesa_callback import SECRET, callback, callback_secret, post_callback, started  # noqa: F401
from tests.test_payments import pending_booking, provider  # noqa: F401  (fixtures)
from tests.test_read_bookings import insert_booking


def test_message_fits_one_segment():
    msg = confirmation_sms("BK7F3AQ9", "Glow Beauty Lounge", ["Classic Haircut"], "Wanjiku",
                           datetime(2026, 10, 5, 14, 30, tzinfo=NBO), 300, "KES")
    assert msg == "Trimmy: BK7F3AQ9 confirmed. Glow Beauty Lounge, Classic Haircut with Wanjiku, Mon 5 Oct 14:30. Deposit KES 300 paid."
    assert len(msg) <= 160


def test_message_variants():
    msg = confirmation_sms("BK1", "Glow", ["Cut", "Colour", "Nails"], None, datetime(2026, 10, 5, 9, 0, tzinfo=NBO), None, "KES")
    assert msg == "Trimmy: BK1 confirmed. Glow, Cut +2 more, Mon 5 Oct 09:00."
    assert "your appointment" in confirmation_sms("BK2", "Glow", [], None, datetime(2026, 10, 5, 9, 0, tzinfo=NBO), None, "KES")


# ─── The task ────────────────────────────────────────────────


@pytest.fixture
def sent(monkeypatch, db_session):
    discover_tasks()
    monkeypatch.setattr(registry, "AsyncSessionLocal", async_sessionmaker(bind=db_session.bind))
    outbox = []
    monkeypatch.setattr(sms, "deliver_sms", lambda to, message, log_hint="": outbox.append((to, message)))
    return outbox


async def run(booking_id):
    await TASKS["booking.send_confirmation"].run({"booking_id": str(booking_id)})


@pytest.mark.asyncio
async def test_sends_once(salon, customer, db_session, sent):
    user, _ = customer
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(days=2), customer_phone="+254722000111")
    await run(b.id)
    await run(b.id)  # a retry or a second worker
    assert len(sent) == 1
    assert sent[0][0] == "+254722000111" and b.reference in sent[0][1] and "Glow Beauty" in sent[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,phone", [("pending_payment", "+254722000111"), ("cancelled", "+254722000111"), ("confirmed", None)])
async def test_skips_what_should_not_be_texted(salon, customer, db_session, sent, status, phone):
    user, _ = customer
    extra = {"deposit_amount": 200, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)} if status == "pending_payment" else {}
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(days=2), status=status,
                             customer_phone=phone, **extra)
    await run(b.id)
    assert sent == []


@pytest.mark.asyncio
async def test_failed_send_is_retried(salon, customer, db_session, sent, monkeypatch):
    user, _ = customer
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(days=2), customer_phone="+254722000111")
    booking_id = b.id

    def gateway_down(to, message, log_hint=""):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(sms, "deliver_sms", gateway_down)
    with pytest.raises(RuntimeError):
        await run(booking_id)
    db_session.expire_all()
    assert (await db_session.get(Booking, booking_id)).confirmation_sent_at is None  # claim released

    monkeypatch.setattr(sms, "deliver_sms", lambda to, message, log_hint="": sent.append((to, message)))
    await run(booking_id)
    assert len(sent) == 1


# ─── When it's triggered ─────────────────────────────────────


@pytest.fixture
def notified(monkeypatch):
    calls = []

    async def record(booking_id):
        calls.append(str(booking_id))

    monkeypatch.setattr(booking_tasks, "notify_confirmed", record)
    return calls


@pytest.mark.asyncio
async def test_no_deposit_booking_is_notified(client, salon, customer, notified):
    _, cust = customer
    booking_id = (await book(client, cust, body(salon))).json()["bookingId"]
    assert notified == [booking_id]


@pytest.mark.asyncio
async def test_deposit_booking_is_notified_when_paid(client, customer, pending_booking, provider, db_session, notified):
    _, cust = customer
    assert notified == []  # pending_payment: not yet
    checkout = await started(client, cust, pending_booking, db_session)
    await post_callback(client, callback(checkout, code=1032))
    assert notified == []  # a failed attempt
    checkout = await started(client, cust, pending_booking, db_session)
    await post_callback(client, callback(checkout))
    assert notified == [pending_booking["bookingId"]]


@pytest.mark.asyncio
async def test_business_bookings(client, auth_headers, salon, notified):
    from tests.test_business_bookings import owner_body
    from tests.test_create_booking import day

    walk_in = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, customerPhone="+254733000111"), headers=auth_headers)
    assert walk_in.status_code == 201 and notified == []  # they're already here
    scheduled = await client.post("/api/v1/bookings/scheduled",
                                  json=owner_body(salon, customerPhone="+254733000111", date=day().isoformat(), time="14:00"),
                                  headers=auth_headers)
    assert notified == [scheduled.json()["bookingId"]]
    no_phone = await client.post("/api/v1/bookings/scheduled",
                                 json=owner_body(salon, date=day().isoformat(), time="15:00"), headers=auth_headers)
    assert no_phone.status_code == 201 and len(notified) == 1
