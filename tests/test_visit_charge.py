"""Checking out a multi-staff visit in one payment, with the tip split between the staff."""
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.modules.booking.models import Booking
from app.modules.business.models import Staff
from app.modules.payments.models import Payment, PaymentAllocation
from app.modules.payouts.models import WalletAdjustment
from tests.test_visits import morning  # noqa: F401  (autouse: a 10:00 clock)
from tests.test_business_bookings import owner_body
from tests.test_charge import charge, checkout_id, till  # noqa: F401  (fixtures)
from tests.test_create_booking import customer, salon  # noqa: F401  (fixtures)
from tests.test_mpesa_callback import callback, callback_secret, post_callback  # noqa: F401  (fixtures)
from tests.test_payments import provider  # noqa: F401  (fixtures)


async def _visit(client, headers, salon, start="now"):
    """Hair (800) with a, nails/colour (2500) with b: a visit of two bookings."""
    hair = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="a", customerPhone="+254733000222"), headers=headers)
    assert hair.status_code == 201, hair.text
    visit = await client.post(
        f"/api/v1/bookings/{hair.json()['bookingId']}/add-service",
        json={"services": [{"serviceId": salon["services"]["colour"], "quantity": 1}], "staffId": salon["b"], "start": start},
        headers=headers,
    )
    assert visit.status_code == 201, visit.text
    return visit.json()


async def _charge_visit(client, headers, visit_id, **json):
    return await client.post(f"/api/v1/visits/{visit_id}/charge", json=json, headers=headers)


async def _commission(db_session, salon, a=40, b=50):
    for key, percent in (("a", a), ("b", b)):
        staff = await db_session.get(Staff, uuid.UUID(salon[key]))
        staff.commission_percent = percent
    await db_session.commit()


async def _tips(db_session, staff_id):
    rows = (await db_session.execute(
        select(WalletAdjustment).where(WalletAdjustment.staff_id == uuid.UUID(staff_id), WalletAdjustment.kind == "tip")
    )).scalars().all()
    return sum(r.amount for r in rows)


@pytest.mark.asyncio
async def test_cash_checkout_settles_everyone_and_splits_tips(client, auth_headers, salon, db_session):
    await _commission(db_session, salon)
    visit = await _visit(client, auth_headers, salon)
    resp = await _charge_visit(client, auth_headers, visit["visitId"], method="cash",
                               tips=[{"staffId": salon["a"], "amount": 100}, {"staffId": salon["b"], "amount": 200}])
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["paymentStatus"] == "paid" and done["balanceDue"]["amount"] == 0
    assert {b["status"] for b in done["bookings"]} == {"completed"}
    assert all(b["paymentMethod"] == "cash" for b in done["bookings"])  # every part shows how it was paid

    # One payment for the lot, split per booking.
    (payment,) = (await db_session.execute(select(Payment).where(Payment.visit_id == uuid.UUID(visit["visitId"])))).scalars().all()
    assert (payment.amount, payment.tip_amount, payment.provider) == (800 + 2500 + 300, 300, "cash")
    shares = {a.booking_id: (a.amount, a.tip_amount) for a in
              (await db_session.execute(select(PaymentAllocation).where(PaymentAllocation.payment_id == payment.id))).scalars()}
    assert sorted(shares.values()) == [(800, 100), (2500, 200)]

    # Each tip in its owner's wallet; commission per booking at each person's rate.
    assert (await _tips(db_session, salon["a"]), await _tips(db_session, salon["b"])) == (100, 200)
    db_session.expire_all()
    rows = (await db_session.execute(select(Booking).where(Booking.visit_id == uuid.UUID(visit["visitId"])))).scalars().all()
    assert sorted((r.total_amount, r.staff_commission, r.tip_amount) for r in rows) == [(800, 320, 100), (2500, 1250, 200)]


@pytest.mark.asyncio
async def test_mpesa_checkout_one_prompt_then_callback(client, auth_headers, salon, till, provider, db_session):
    visit = await _visit(client, auth_headers, salon)
    resp = await _charge_visit(client, auth_headers, visit["visitId"], method="mpesa", tips=[{"staffId": salon["b"], "amount": 150}])
    assert resp.status_code == 202, resp.text
    started = resp.json()
    assert started["visitId"] == visit["visitId"]
    assert provider.calls[-1]["amount"] == 800 + 2500 + 150
    assert len(provider.calls) == 1  # one prompt for the whole visit

    pending = (await client.get(f"/api/v1/visits/{visit['visitId']}", headers=auth_headers)).json()
    assert pending["chargePayment"]["status"] == "pending" and pending["paymentStatus"] == "unpaid"
    # While it's waiting, neither the visit nor one of its parts can be charged again.
    assert (await _charge_visit(client, auth_headers, visit["visitId"], method="mpesa")).json()["error"] == "payment_in_progress"
    assert (await charge(client, auth_headers, visit["bookings"][0]["bookingId"], method="cash")).json()["error"] == "payment_in_progress"

    cb = callback(await checkout_id(db_session, started["paymentId"]), receipt="VIS12345XY", amount=3450)
    await post_callback(client, cb)
    await post_callback(client, cb)  # Daraja retries: applied once
    done = (await client.get(f"/api/v1/visits/{visit['visitId']}", headers=auth_headers)).json()
    assert done["paymentStatus"] == "paid"
    assert {b["paymentReference"] for b in done["bookings"]} == {"VIS12345XY"}
    assert (await _tips(db_session, salon["a"]), await _tips(db_session, salon["b"])) == (0, 150)


@pytest.mark.asyncio
async def test_not_ready_until_everything_started_or_charge_the_rest(client, auth_headers, salon):
    visit = await _visit(client, auth_headers, salon, start="after")
    resp = await _charge_visit(client, auth_headers, visit["visitId"], method="cash")
    assert resp.status_code == 409 and resp.json()["error"] == "visit_not_ready"
    waiting = resp.json()["bookingIds"]
    assert len(waiting) == 1

    ready = visit["chargeableBookingIds"]
    partial = await _charge_visit(client, auth_headers, visit["visitId"], method="cash", bookingIds=ready)
    assert partial.status_code == 200, partial.text
    assert partial.json()["paymentStatus"] == "partially_paid"
    assert partial.json()["balanceDue"]["amount"] == 2500

    # Start the other part, then charge what's left.
    await client.patch(f"/api/v1/bookings/{waiting[0]}/status", json={"status": "in_progress"}, headers=auth_headers)
    rest = await _charge_visit(client, auth_headers, visit["visitId"], method="cash")
    assert rest.status_code == 200, rest.text
    assert rest.json()["paymentStatus"] == "paid"
    assert (await _charge_visit(client, auth_headers, visit["visitId"], method="cash")).json()["error"] == "already_paid"


@pytest.mark.asyncio
async def test_tip_rules(client, auth_headers, salon, db_session):
    visit = await _visit(client, auth_headers, salon)
    outsider = str(uuid.uuid4())
    bad = await _charge_visit(client, auth_headers, visit["visitId"], method="cash", tips=[{"staffId": outsider, "amount": 50}])
    assert bad.status_code == 400 and bad.json()["error"] == "tip_staff_not_in_visit"
    twice = await _charge_visit(client, auth_headers, visit["visitId"], method="cash",
                                tips=[{"staffId": salon["a"], "amount": 50}, {"staffId": salon["a"], "amount": 50}])
    assert twice.status_code == 400


@pytest.mark.asyncio
async def test_part_paid_separately_during_prompt_is_refunded(client, auth_headers, salon, till, provider, db_session):
    visit = await _visit(client, auth_headers, salon)
    started = (await _charge_visit(client, auth_headers, visit["visitId"], method="mpesa")).json()
    hair_id = next(b["bookingId"] for b in visit["bookings"] if b["staffId"] == salon["a"])

    # Someone took cash for the hair while the prompt was out (after the lock ran out).
    hair = await db_session.get(Booking, uuid.UUID(hair_id))
    hair.payment_status = "paid"
    await db_session.commit()

    await post_callback(client, callback(await checkout_id(db_session, started["paymentId"]), receipt="DUP12345XY", amount=3300))
    db_session.expire_all()
    hair = await db_session.get(Booking, uuid.UUID(hair_id))
    assert (hair.refund_amount, hair.refund_status) == (800, "due")
    done = (await client.get(f"/api/v1/visits/{visit['visitId']}", headers=auth_headers)).json()
    assert done["paymentStatus"] == "paid"


@pytest.mark.asyncio
async def test_earnings_split_by_staff(client, auth_headers, salon, db_session):
    await _commission(db_session, salon)
    visit = await _visit(client, auth_headers, salon)
    await _charge_visit(client, auth_headers, visit["visitId"], method="cash",
                        tips=[{"staffId": salon["a"], "amount": 100}, {"staffId": salon["b"], "amount": 200}])
    today = datetime.now(ZoneInfo("Africa/Nairobi")).date().isoformat()  # walk-ins are today
    params = {"from": today, "to": today}
    for key, revenue, tips in (("a", 800, 100), ("b", 2500, 200)):
        mine = (await client.get(f"/api/v1/businesses/{salon['id']}/earnings", params={**params, "staffId": salon[key]}, headers=auth_headers)).json()
        assert mine["totalRevenue"] == revenue, mine
        assert mine["money"]["tips"] == tips, mine["money"]
