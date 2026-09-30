import uuid
from datetime import datetime, timedelta, timezone

import pytest

import app.modules.notifications.tasks as notification_tasks
from app.modules.booking.models import Booking
from app.modules.business.models import Business, Staff
from app.modules.payments.models import Payment
from tests.test_business_bookings import owner_body
from tests.test_create_booking import NBO, customer, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, insert_booking, make_user


def today():
    return datetime.now(NBO).date()


@pytest.fixture
async def desk(salon, db_session):
    user = await make_user(db_session, "desk.day@example.com")
    db_session.add(Staff(id=uuid.uuid4(), business_id=uuid.UUID(salon["id"]), user_id=user.id, name="Faith",
                         role="front_desk", performs_all_services=False))
    await db_session.commit()
    return headers_for(user)


@pytest.fixture
def told(monkeypatch):
    calls = {"sms": [], "push": []}

    async def sms(to, message):
        calls["sms"].append((to, message))

    async def push(user_id, title, body, **data):
        calls["push"].append((user_id, title, data.get("type")))

    monkeypatch.setattr(notification_tasks, "send_sms", sms)
    monkeypatch.setattr(notification_tasks, "notify", push)
    return calls


async def summary(client, headers, salon, day=None):
    resp = await client.get(f"/api/v1/businesses/{salon['id']}/day-summary",
                            params={"date": (day or today()).isoformat()}, headers=headers)
    return resp


@pytest.mark.asyncio
async def test_the_day_in_numbers(client, auth_headers, salon, customer, desk, db_session):
    user, _ = customer
    # Two walk-ins: one paid cash, one still unpaid in the chair.
    paid = (await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="a"), headers=auth_headers)).json()
    await client.post(f"/api/v1/bookings/{paid['bookingId']}/charge", json={"method": "cash"}, headers=auth_headers)
    await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="b", customerName="Baraka"), headers=auth_headers)
    # One appointment booked ahead with a 300 M-Pesa deposit paid today, and one cancelled.
    now = datetime.now(timezone.utc)
    appt = await insert_booking(db_session, salon, user.id, now, status="confirmed")
    appt.deposit_amount, appt.payment_status = 300, "deposit_paid"
    db_session.add(Payment(booking_id=appt.id, kind="deposit", provider="mpesa", amount=300, phone="254722000111",
                           destination_type="mpesa_till", party_b="123456", account_reference="BK1", status="succeeded",
                           settled_at=now))
    gone = await insert_booking(db_session, salon, user.id, now, status="cancelled")
    gone.refund_amount, gone.refund_status, gone.refunded_at = 300, "refunded", now
    await db_session.commit()

    resp = await summary(client, desk, salon)  # front desk can read it
    assert resp.status_code == 200, resp.text
    s = resp.json()
    assert (s["appointments"], s["walkIns"], s["completed"], s["cancelled"], s["noShows"]) == (1, 2, 1, 1, 0)
    assert s["collected"]["cash"] == {"amount": 800, "count": 1}
    assert s["collected"]["mpesa"] == {"amount": 300, "count": 1}
    assert s["collected"]["depositsCollected"] == {"amount": 300, "count": 1}
    assert s["collected"]["balancePayments"] == {"amount": 800, "count": 1}
    assert s["refundsPaid"] == {"amount": 300, "count": 1}
    assert [(u["customerName"], u["status"], u["balanceDue"]["amount"]) for u in s["unpaid"]] == [("Baraka", "in_progress", 800)]
    assert s["submission"] == {"submittedAt": None, "submittedBy": None, "notes": None}


@pytest.mark.asyncio
async def test_money_counts_on_the_day_it_came_in(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    yesterday_visit = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) - timedelta(days=1), status="completed")
    db_session.add(Payment(booking_id=yesterday_visit.id, kind="charge", provider="cash", amount=800, phone="",
                           destination_type="cash", party_b="", account_reference="BK2", status="succeeded",
                           settled_at=datetime.now(timezone.utc)))
    await db_session.commit()
    s = (await summary(client, auth_headers, salon)).json()
    assert (s["collected"]["cash"]["amount"], s["completed"]) == (800, 0)  # in today's drawer, not today's visits


@pytest.mark.asyncio
async def test_submit_tells_the_owner_and_can_be_redone(client, auth_headers, salon, desk, told, db_session):
    from app.modules.auth.models import User

    business = await db_session.get(Business, uuid.UUID(salon["id"]))
    owner_id = business.owner_id
    owner = await db_session.get(User, owner_id)
    owner.phone = "+254700111222"
    await db_session.commit()
    url = f"/api/v1/businesses/{salon['id']}/day-summary/{today().isoformat()}/submit"

    first = await client.post(url, json={"notes": "KSh 200 short due to change"}, headers=desk)
    assert first.status_code == 200, first.text
    sub = first.json()["submission"]
    assert (sub["submittedBy"], sub["notes"]) == ("Faith", "KSh 200 short due to change")
    assert sub["submittedAt"] is not None
    assert told["sms"][0][0] == "+254700111222" and "Faith submitted" in told["sms"][0][1]
    assert told["push"] == [(owner_id, "Day summary submitted", "day_summary_submitted")]

    again = (await client.post(url, json={"notes": "Found the 200"}, headers=desk)).json()
    assert again["submission"]["notes"] == "Found the 200"
    assert (await summary(client, auth_headers, salon)).json()["submission"]["notes"] == "Found the 200"


@pytest.mark.asyncio
async def test_owner_submitting_does_not_notify_themselves(client, auth_headers, salon, told):
    url = f"/api/v1/businesses/{salon['id']}/day-summary/{today().isoformat()}/submit"
    assert (await client.post(url, json={}, headers=auth_headers)).status_code == 200
    assert told == {"sms": [], "push": []}


@pytest.mark.asyncio
async def test_rules_and_permissions(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    tomorrow = (today() + timedelta(days=1)).isoformat()
    future = await client.post(f"/api/v1/businesses/{salon['id']}/day-summary/{tomorrow}/submit", json={}, headers=auth_headers)
    assert future.status_code == 400
    assert (await summary(client, cust, salon)).status_code == 404
    stylist = await make_user(db_session, "stylist.day@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id = stylist.id
    await db_session.commit()
    assert (await summary(client, headers_for(stylist), salon)).status_code == 403
