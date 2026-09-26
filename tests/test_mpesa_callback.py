import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.modules.booking.models import Booking, BookingEvent
from app.modules.booking.states import transition
from app.modules.payments.models import Payment
from tests.test_create_booking import body, book, customer, salon, set_deposit  # noqa: F401  (fixtures)
from tests.test_payments import pay, pending_booking, provider  # noqa: F401  (fixtures)

SECRET = "callback-secret-for-tests"


@pytest.fixture(autouse=True)
def callback_secret(monkeypatch):
    monkeypatch.setattr(settings, "MPESA_CALLBACK_SECRET", SECRET)


def callback(checkout_id, code=0, receipt="SJK4R2T8QX", amount=300):
    cb = {"MerchantRequestID": "m-1", "CheckoutRequestID": checkout_id, "ResultCode": code,
          "ResultDesc": "The service request is processed successfully." if code == 0 else "Request cancelled by user"}
    if code == 0:
        cb["CallbackMetadata"] = {"Item": [
            {"Name": "Amount", "Value": float(amount)}, {"Name": "MpesaReceiptNumber", "Value": receipt},
            {"Name": "TransactionDate", "Value": 20260927101500}, {"Name": "PhoneNumber", "Value": 254722000111},
        ]}
    return {"Body": {"stkCallback": cb}}


async def post_callback(client, payload):
    resp = await client.post(f"/api/v1/payments/mpesa/callback/{SECRET}", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}


async def started(client, cust, booking, db_session):
    resp = await pay(client, cust, booking["bookingId"])
    assert resp.status_code == 202, resp.text
    payment = await db_session.get(Payment, uuid.UUID(resp.json()["paymentId"]))
    return payment.checkout_request_id


async def get_booking(client, cust, booking):
    return (await client.get(f"/api/v1/bookings/{booking['bookingId']}", headers=cust)).json()


async def reasons(db_session, booking_id):
    db_session.expire_all()
    return (await db_session.execute(
        select(BookingEvent.reason).where(BookingEvent.booking_id == uuid.UUID(booking_id)).order_by(BookingEvent.at)
    )).scalars().all()


@pytest.mark.asyncio
async def test_success_confirms_the_booking(client, customer, pending_booking, provider, db_session):
    _, cust = customer
    checkout = await started(client, cust, pending_booking, db_session)
    await post_callback(client, callback(checkout))

    b = await get_booking(client, cust, pending_booking)
    assert (b["status"], b["paymentStatus"], b["expiresAt"]) == ("confirmed", "deposit_paid", None)
    assert (b["paymentReference"], b["paymentMethod"]) == ("SJK4R2T8QX", "mpesa")
    assert b["depositPayment"]["status"] == "succeeded"
    assert (await reasons(db_session, pending_booking["bookingId"]))[-1] == "mpesa:SJK4R2T8QX"

    payment = (await db_session.execute(select(Payment).where(Payment.checkout_request_id == checkout))).scalar_one()
    assert payment.raw_callback["Body"]["stkCallback"]["ResultCode"] == 0
    assert payment.settled_at is not None


@pytest.mark.asyncio
async def test_duplicate_callbacks_change_nothing(client, customer, pending_booking, provider, db_session):
    _, cust = customer
    checkout = await started(client, cust, pending_booking, db_session)
    await post_callback(client, callback(checkout))
    before = await reasons(db_session, pending_booking["bookingId"])
    await post_callback(client, callback(checkout))
    await post_callback(client, callback(checkout, code=1032))  # a late contradicting one too
    assert await reasons(db_session, pending_booking["bookingId"]) == before
    assert (await get_booking(client, cust, pending_booking))["status"] == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status,reason", [(1032, "failed", "cancelled"), (1037, "timeout", "timeout"), (2001, "failed", "wrong_pin")])
async def test_failure_leaves_the_booking_payable(client, customer, pending_booking, provider, db_session, code, status, reason):
    _, cust = customer
    checkout = await started(client, cust, pending_booking, db_session)
    await post_callback(client, callback(checkout, code=code))

    b = await get_booking(client, cust, pending_booking)
    assert b["status"] == "pending_payment"
    assert b["depositPayment"]["status"] == status and b["depositPayment"]["failureReason"] == reason
    assert (await pay(client, cust, pending_booking["bookingId"])).status_code == 202  # can try again at once


@pytest.mark.asyncio
async def test_unknown_checkout_is_still_acknowledged(client):
    await post_callback(client, callback("ws_CO_nobody"))


@pytest.mark.asyncio
async def test_late_payment_reinstates_if_the_slot_is_free(client, customer, pending_booking, provider, db_session):
    _, cust = customer
    checkout = await started(client, cust, pending_booking, db_session)
    b = await db_session.get(Booking, uuid.UUID(pending_booking["bookingId"]))
    await db_session.refresh(b, ["events"])
    transition(b, "cancelled", reason="payment_expired")  # the sweep got there first
    await db_session.commit()

    await post_callback(client, callback(checkout))
    b = await get_booking(client, cust, pending_booking)
    assert (b["status"], b["cancelReason"], b["paymentStatus"]) == ("confirmed", None, "deposit_paid")
    assert (await reasons(db_session, pending_booking["bookingId"]))[-1].startswith("late_payment")


@pytest.mark.asyncio
async def test_late_payment_when_the_slot_was_taken_is_flagged_for_refund(client, auth_headers, customer, salon, pending_booking, provider, db_session):
    _, cust = customer
    checkout = await started(client, cust, pending_booking, db_session)
    b = await db_session.get(Booking, uuid.UUID(pending_booking["bookingId"]))
    await db_session.refresh(b, ["events"])
    transition(b, "cancelled", reason="payment_expired")
    await db_session.commit()
    # Someone else books the same staff member at the same time (no deposit needed now).
    await client.put(
        f"/api/v1/businesses/{salon['id']}/policies",
        json={"deposit": {"required": False, "type": "fixed", "appliesTo": "all_services"},
              "cancellation": {"freeCancellationHours": 24, "lateFeePercent": 50}, "noShow": {"feePercent": 100}},
        headers=auth_headers,
    )
    assert (await book(client, cust, body(salon))).status_code == 201

    await post_callback(client, callback(checkout))
    b = await get_booking(client, cust, pending_booking)
    assert (b["status"], b["cancelReason"], b["paymentStatus"]) == ("cancelled", "payment_expired", "deposit_paid")
    assert (await reasons(db_session, pending_booking["bookingId"]))[-1] == "late_payment_refund_due"
    assert (b["refund"]["amount"]["amount"], b["refund"]["status"]) == (300, "due")  # the business refunds it (D10)

