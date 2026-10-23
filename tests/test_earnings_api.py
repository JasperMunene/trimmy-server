"""GET /businesses/{id}/earnings against real bookings, charges and payments."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.booking.models import Booking
from app.modules.business.models import Staff
from tests.test_business_bookings import owner_body
from tests.test_create_booking import NBO, customer, salon  # noqa: F401  (fixtures)
from tests.test_mpesa_callback import callback, callback_secret, post_callback  # noqa: F401  (fixtures)
from tests.test_payments import provider  # noqa: F401  (fixtures)
from tests.test_read_bookings import PATHS, headers_for, insert_booking, make_user


def today():
    return datetime.now(NBO).date()


async def cash_visit(client, auth_headers, salon, staff="a", services=(("cut", 1),)):
    b = (await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff=staff, services=services, customerName="Baraka"),
                           headers=auth_headers)).json()
    assert (await client.post(f"/api/v1/bookings/{b['bookingId']}/charge", json={"method": "cash"}, headers=auth_headers)).status_code == 200
    return b["bookingId"]


async def earnings(client, headers, salon, **params):
    params.setdefault("from", today().isoformat())
    params.setdefault("to", today().isoformat())
    return await client.get(f"/api/v1/businesses/{salon['id']}/earnings", params=params, headers=headers)


@pytest.fixture
async def rates(salon, db_session):
    for key, pct in (("a", 40), ("b", 50)):
        member = await db_session.get(Staff, uuid.UUID(salon[key]))
        member.commission_percent = pct
    await db_session.commit()


@pytest.mark.asyncio
async def test_today_for_the_whole_business(client, auth_headers, salon, rates, provider, db_session):
    await client.put(f"/api/v1/businesses/{salon['id']}/payment-destination",
                     json={"type": "mpesa_till", "tillNumber": "123456"}, headers=auth_headers)
    await cash_visit(client, auth_headers, salon, staff="a")  # cut 800, cash, A
    await cash_visit(client, auth_headers, salon, staff="b", services=(("cut", 1), ("colour", 1)))  # 3300, cash, B
    # One paid by M-Pesa at checkout.
    m = (await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff=None), headers=auth_headers)).json()
    started = (await client.post(f"/api/v1/bookings/{m['bookingId']}/charge", json={"method": "mpesa", "phone": "0722000111"}, headers=auth_headers)).json()
    from app.modules.payments.models import Payment
    checkout = (await db_session.get(Payment, uuid.UUID(started["paymentId"]))).checkout_request_id
    await post_callback(client, callback(checkout, receipt="EARN1234", amount=800))
    # And one still in progress: no money yet.
    await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff=None), headers=auth_headers)

    resp = await earnings(client, auth_headers, salon)
    assert resp.status_code == 200, resp.text
    e = resp.json()
    assert (e["totalRevenue"], e["paidBookings"], e["completed"], e["avgTicket"]) == (4900, 3, 3, 1633)
    assert (e["granularity"], len(e["buckets"]), e["timezone"], e["currency"]) == ("hour", 8, "Africa/Nairobi", "KES")
    assert sum(b["amount"] for b in e["buckets"]) == 4900
    assert e["paymentMethods"] == [{"method": "mpesa", "amount": 800, "count": 1}, {"method": "cash", "amount": 4100, "count": 2}]
    assert [(s["name"], s["amount"], s["bookingsCount"]) for s in e["services"]] == [("cut", 2400, 3), ("colour", 2500, 1)][::-1]
    assert len(e["transactions"]) == e["transactionsTotal"] == 4
    assert all(t["date"] == today().isoformat() for t in e["transactions"])
    assert {t["paymentMethod"] for t in e["transactions"]} == {"cash", "mpesa"}
    assert [(s["name"], s["amount"], s["commission"]) for s in e["staff"]] == [("Otieno", 3300, 1650), ("Wanjiku", 800, 320), ("Unassigned", 800, 0)]
    assert e["commission"] == 1970
    assert {(c["method"], c["kind"]) for c in e["money"]["collected"]} == {("cash", "charge"), ("mpesa", "charge")}


@pytest.mark.asyncio
async def test_scoped_to_one_staff_member(client, auth_headers, salon, rates):
    await cash_visit(client, auth_headers, salon, staff="a")
    await cash_visit(client, auth_headers, salon, staff="b")
    e = (await earnings(client, auth_headers, salon, staffId=salon["a"])).json()
    assert (e["totalRevenue"], e["commission"], e["staffId"], e["staff"]) == (800, 320, salon["a"], [])
    assert len(e["services"]) == 2  # still every service, for "what isn't selling"


@pytest.mark.asyncio
async def test_staff_see_only_themselves(client, auth_headers, salon, rates, db_session):
    await cash_visit(client, auth_headers, salon, staff="a")
    await cash_visit(client, auth_headers, salon, staff="b")
    user = await make_user(db_session, "earner@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id = user.id
    await db_session.commit()
    h = headers_for(user)

    mine = (await earnings(client, h, salon)).json()
    assert (mine["totalRevenue"], mine["staffId"]) == (800, salon["a"])  # scoped automatically
    assert (await earnings(client, h, salon, staffId=salon["b"])).status_code == 403

    desk = await make_user(db_session, "desk.earnings@example.com")
    db_session.add(Staff(id=uuid.uuid4(), business_id=uuid.UUID(salon["id"]), user_id=desk.id, name="Desk", role="front_desk"))
    await db_session.commit()
    assert (await earnings(client, headers_for(desk), salon)).status_code == 403


@pytest.mark.asyncio
async def test_week_view_with_previous_period(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    now = datetime.now(timezone.utc)

    async def paid(starts, total):
        b = await insert_booking(db_session, salon, user.id, starts, status="completed")
        b.payment_status, b.total_amount = "paid", total
        await db_session.commit()

    await paid(now - timedelta(days=2), 1000)   # this week
    await paid(now - timedelta(days=1), 2000)   # this week
    await paid(now - timedelta(days=9), 2000)   # the week before
    no_show = await insert_booking(db_session, salon, user.id, now - timedelta(days=3), status="no_show")

    start = today() - timedelta(days=6)
    e = (await earnings(client, auth_headers, salon, **{"from": start.isoformat(), "to": today().isoformat()})).json()
    assert (e["granularity"], len(e["buckets"])) == ("day", 7)
    assert (e["totalRevenue"], e["completed"], e["noShow"]) == (3000, 2, 1)
    p = e["previous"]
    assert (p["from"], p["to"]) == ((start - timedelta(days=7)).isoformat(), (start - timedelta(days=1)).isoformat())
    assert (p["totalRevenue"], p["revenueChangePercent"]) == (2000, 50.0)


@pytest.mark.asyncio
async def test_cancellations_and_refunds_show_under_money(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    now = datetime.now(timezone.utc)
    kept = await insert_booking(db_session, salon, user.id, now + timedelta(hours=3), status="cancelled")
    kept.cancellation_fee = 300
    owed = await insert_booking(db_session, salon, user.id, now + timedelta(hours=4), status="cancelled")
    owed.refund_amount, owed.refund_status = 500, "due"
    await db_session.commit()
    e = (await earnings(client, auth_headers, salon, **{"from": today().isoformat(), "to": (today() + timedelta(days=1)).isoformat()})).json()
    assert (e["cancelled"], e["money"]["cancellationFees"], e["money"]["refundsDue"], e["totalRevenue"]) == (2, 300, 500, 0)


@pytest.mark.asyncio
async def test_transactions_limit(client, auth_headers, salon):
    for _ in range(3):
        await cash_visit(client, auth_headers, salon)
    e = (await earnings(client, auth_headers, salon, transactionsLimit=2)).json()
    assert (len(e["transactions"]), e["transactionsTotal"]) == (2, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("params,status", [
    ({"from": "2026-10-10", "to": "2026-10-01"}, 400),
    ({"from": "2025-01-01", "to": "2026-06-01"}, 400),
    ({"transactionsLimit": -1}, 400),
    ({"staffId": str(uuid.uuid4())}, 404),
])
async def test_validation(client, auth_headers, salon, params, status):
    assert (await earnings(client, auth_headers, salon, **params)).status_code == status


@pytest.mark.asyncio
async def test_customers_cannot_see_earnings(client, salon, customer):
    _, cust = customer
    assert (await earnings(client, cust, salon)).status_code == 404
