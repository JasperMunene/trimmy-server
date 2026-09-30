import uuid

import pytest
from sqlalchemy import select

from app.modules.booking.models import Booking
from app.modules.booking.states import transition
from app.modules.business.models import Staff
from app.modules.payments.models import Payment
from tests.test_business_bookings import owner_body
from tests.test_create_booking import body, book, customer, salon, set_deposit  # noqa: F401  (fixtures)
from tests.test_mpesa_callback import callback, callback_secret, post_callback  # noqa: F401  (fixtures)
from tests.test_payments import provider  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


@pytest.fixture
async def till(client, auth_headers, salon):
    await client.put(f"/api/v1/businesses/{salon['id']}/payment-destination",
                     json={"type": "mpesa_till", "tillNumber": "123456"}, headers=auth_headers)


async def walk_in(client, auth_headers, salon):
    resp = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, customerPhone="+254733000111"), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["bookingId"]


async def charge(client, headers, booking_id, **json):
    return await client.post(f"/api/v1/bookings/{booking_id}/charge", json=json, headers=headers)


async def get(client, headers, booking_id):
    return (await client.get(f"/api/v1/bookings/{booking_id}", headers=headers)).json()


async def checkout_id(db_session, payment_id):
    return (await db_session.get(Payment, uuid.UUID(payment_id))).checkout_request_id


@pytest.mark.asyncio
async def test_cash_completes_at_once(client, auth_headers, salon, db_session):
    booking_id = await walk_in(client, auth_headers, salon)
    assert (await get(client, auth_headers, booking_id))["balanceDue"] == {"amount": 800, "currency": "KES"}

    resp = await charge(client, auth_headers, booking_id, method="cash")
    assert resp.status_code == 200, resp.text
    b = resp.json()
    assert (b["status"], b["paymentStatus"], b["paymentMethod"], b["paymentReference"]) == ("completed", "paid", "cash", None)
    assert b["balanceDue"]["amount"] == 0
    assert b["chargePayment"]["status"] == "succeeded"
    payment = (await db_session.execute(select(Payment).where(Payment.booking_id == uuid.UUID(booking_id)))).scalar_one()
    assert (payment.provider, payment.kind, payment.amount) == ("cash", "charge", 800)


@pytest.mark.asyncio
async def test_mpesa_charge_end_to_end(client, auth_headers, salon, till, provider, db_session):
    booking_id = await walk_in(client, auth_headers, salon)
    resp = await charge(client, auth_headers, booking_id, method="mpesa")
    assert resp.status_code == 202, resp.text
    started = resp.json()
    assert (started["status"], started["amount"], started["phone"], started["expiresAt"]) == ("pending", 800, "254733000111", None)
    assert provider.calls[-1]["amount"] == 800

    mid = await get(client, auth_headers, booking_id)
    assert (mid["status"], mid["chargePayment"]["status"], mid["balanceDue"]["amount"]) == ("in_progress", "pending", 800)

    await post_callback(client, callback(await checkout_id(db_session, started["paymentId"]), receipt="CHG12345XY", amount=800))
    done = await get(client, auth_headers, booking_id)
    assert (done["status"], done["paymentStatus"], done["paymentMethod"], done["paymentReference"]) == ("completed", "paid", "mpesa", "CHG12345XY")
    assert done["balanceDue"]["amount"] == 0 and done["chargePayment"]["status"] == "succeeded"
    assert (await charge(client, auth_headers, booking_id, method="cash")).json()["error"] == "already_paid"


@pytest.mark.asyncio
async def test_failed_charge_can_be_retried(client, auth_headers, salon, till, provider, db_session):
    booking_id = await walk_in(client, auth_headers, salon)
    first = (await charge(client, auth_headers, booking_id, method="mpesa")).json()
    assert (await charge(client, auth_headers, booking_id, method="mpesa")).json()["error"] == "payment_in_progress"
    await post_callback(client, callback(await checkout_id(db_session, first["paymentId"]), code=1032))
    b = await get(client, auth_headers, booking_id)
    assert (b["status"], b["chargePayment"]["status"], b["chargePayment"]["failureReason"]) == ("in_progress", "failed", "cancelled")
    assert (await charge(client, auth_headers, booking_id, method="mpesa", phone="0711000999")).status_code == 202


@pytest.fixture
async def paid_deposit_in_progress(client, auth_headers, salon, customer, db_session):
    """A customer booking whose 300 deposit was paid, now checked in."""
    _, cust = customer
    await set_deposit(client, auth_headers, salon, {"required": True, "type": "fixed", "amount": {"amount": 300, "currency": "KES"}, "appliesTo": "all_services"})
    booking_id = (await book(client, cust, body(salon))).json()["bookingId"]
    b = await db_session.get(Booking, uuid.UUID(booking_id))
    await db_session.refresh(b, ["events"])
    transition(b, "confirmed")
    b.payment_status = "deposit_paid"
    transition(b, "in_progress")
    await db_session.commit()
    return booking_id


@pytest.mark.asyncio
async def test_deposit_is_taken_off_the_balance(client, auth_headers, till, provider, paid_deposit_in_progress):
    assert (await get(client, auth_headers, paid_deposit_in_progress))["balanceDue"]["amount"] == 500
    resp = await charge(client, auth_headers, paid_deposit_in_progress, method="mpesa")
    assert resp.status_code == 202 and resp.json()["amount"] == 500


@pytest.mark.asyncio
async def test_nothing_left_to_pay_completes_even_for_mpesa(client, auth_headers, salon, till, provider, paid_deposit_in_progress, db_session):
    b = await db_session.get(Booking, uuid.UUID(paid_deposit_in_progress))
    b.deposit_amount = b.total_amount  # the deposit covered everything
    await db_session.commit()
    resp = await charge(client, auth_headers, paid_deposit_in_progress, method="mpesa")
    assert resp.status_code == 200
    assert (resp.json()["status"], resp.json()["paymentStatus"]) == ("completed", "paid")
    assert provider.calls == []


@pytest.mark.asyncio
async def test_paid_twice_leaves_a_refund_owed(client, auth_headers, salon, till, provider, db_session):
    booking_id = await walk_in(client, auth_headers, salon)
    started = (await charge(client, auth_headers, booking_id, method="mpesa")).json()
    assert (await charge(client, auth_headers, booking_id, method="cash")).status_code == 200  # paid in cash meanwhile
    await post_callback(client, callback(await checkout_id(db_session, started["paymentId"]), amount=800))
    b = await get(client, auth_headers, booking_id)
    assert (b["paymentMethod"], b["refund"]["amount"]["amount"], b["refund"]["status"]) == ("cash", 800, "due")


@pytest.mark.asyncio
async def test_only_in_progress_bookings_can_be_charged(client, auth_headers, salon, customer):
    _, cust = customer
    confirmed = (await book(client, cust, body(salon))).json()["bookingId"]
    resp = await charge(client, auth_headers, confirmed, method="cash")
    assert resp.status_code == 409 and resp.json()["error"] == "not_in_progress"


@pytest.mark.asyncio
async def test_who_can_charge(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    booking_id = await walk_in(client, auth_headers, salon)
    assert (await charge(client, cust, booking_id, method="cash")).status_code == 404  # a customer
    stylist = await make_user(db_session, "charger@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id = stylist.id
    await db_session.commit()
    assert (await charge(client, headers_for(stylist), booking_id, method="cash")).status_code == 403
    assert (await charge(client, auth_headers, booking_id, method="card")).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["cash", "mpesa"])
async def test_a_completed_but_unpaid_visit_can_still_be_charged(client, auth_headers, salon, till, provider, db_session, method):
    """Staff may mark their own booking completed; checkout afterwards must still work."""
    booking_id = await walk_in(client, auth_headers, salon)
    done = await client.patch(f"/api/v1/bookings/{booking_id}/status", json={"status": "completed"}, headers=auth_headers)
    assert (done.json()["status"], done.json()["paymentStatus"], done.json()["balanceDue"]["amount"]) == ("completed", "unpaid", 800)

    resp = await charge(client, auth_headers, booking_id, method=method)
    if method == "cash":
        assert resp.status_code == 200
    else:
        assert resp.status_code == 202
        await post_callback(client, callback(await checkout_id(db_session, resp.json()["paymentId"]), amount=800))
    b = await get(client, auth_headers, booking_id)
    assert (b["status"], b["paymentStatus"], b["balanceDue"]["amount"]) == ("completed", "paid", 0)


@pytest.mark.asyncio
async def test_other_statuses_still_cannot_be_charged(client, auth_headers, salon, customer):
    _, cust = customer
    cancelled = (await book(client, cust, body(salon))).json()["bookingId"]
    await client.post(f"/api/v1/bookings/{cancelled}/cancel", json={}, headers=cust)
    resp = await charge(client, auth_headers, cancelled, method="cash")
    assert resp.status_code == 409 and resp.json()["error"] == "not_in_progress"
