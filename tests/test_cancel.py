import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.booking.cancellation import outcome
from app.modules.booking.models import Booking
from app.modules.booking.states import transition
from tests.test_create_booking import body, book, customer, salon, set_deposit  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


# ─── The rules ───────────────────────────────────────────────


@pytest.mark.parametrize("hours_before,deposit,total,expected", [
    (48, 300, 800, (0, 300)),    # inside the free window: all back
    (24, 300, 800, (0, 300)),    # exactly at the edge still counts as free
    (10, 300, 800, (300, 0)),    # late: 50% of 800 = 400, capped at the 300 deposit
    (10, 500, 800, (400, 100)),  # late: 400 kept, 100 back
    (10, 0, 800, (0, 0)),        # no deposit paid: free
])
def test_customer_outcomes(hours_before, deposit, total, expected):
    result = outcome(status="confirmed", starts_at=NOW + timedelta(hours=hours_before), total=total,
                     deposit_paid=deposit, free_hours=24, late_fee_percent=50, now=NOW)
    assert result.can_cancel and (result.fee, result.refund) == expected


@pytest.mark.parametrize("status,starts_in,blocked", [
    ("confirmed", timedelta(hours=-1), True),  # already started
    ("completed", timedelta(hours=5), True),
    ("cancelled", timedelta(hours=5), True),
    ("pending_payment", timedelta(hours=5), False),
])
def test_what_can_be_cancelled(status, starts_in, blocked):
    result = outcome(status=status, starts_at=NOW + starts_in, total=800, deposit_paid=0,
                     free_hours=24, late_fee_percent=50, now=NOW)
    assert result.can_cancel is not blocked


def test_business_cancelling_refunds_everything():
    result = outcome(status="confirmed", starts_at=NOW + timedelta(hours=1), total=800, deposit_paid=300,
                     free_hours=24, late_fee_percent=50, now=NOW, by_business=True)
    assert (result.fee, result.refund) == (0, 300)


# ─── The endpoints ───────────────────────────────────────────


async def paid_booking(client, auth_headers, salon, cust, db_session, deposit=300, free_hours=24, late_fee=50):
    await client.put(
        f"/api/v1/businesses/{salon['id']}/policies",
        json={"deposit": {"required": True, "type": "fixed", "amount": {"amount": deposit, "currency": "KES"}, "appliesTo": "all_services"},
              "cancellation": {"freeCancellationHours": free_hours, "lateFeePercent": late_fee}, "noShow": {"feePercent": 100}},
        headers=auth_headers,
    )
    booking_id = (await book(client, cust, body(salon))).json()["bookingId"]
    b = await db_session.get(Booking, uuid.UUID(booking_id))
    await db_session.refresh(b, ["events"])
    transition(b, "confirmed", reason="mpesa:TEST")  # as if the deposit was paid
    b.payment_status = "deposit_paid"
    await db_session.commit()
    return booking_id


async def preview(client, headers, booking_id):
    return await client.get(f"/api/v1/bookings/{booking_id}/cancel-preview", headers=headers)


async def cancel(client, headers, booking_id, **json):
    return await client.post(f"/api/v1/bookings/{booking_id}/cancel", json=json, headers=headers)


@pytest.mark.asyncio
async def test_free_cancellation_leaves_a_refund_due(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    booking_id = await paid_booking(client, auth_headers, salon, cust, db_session)  # a week out, 24h window

    p = (await preview(client, cust, booking_id)).json()
    assert (p["canCancel"], p["withinFreeWindow"], p["fee"]["amount"], p["refund"]["amount"]) == (True, True, 0, 300)
    assert "refunded by the business" in p["message"]

    b = (await cancel(client, cust, booking_id, reason="Something came up")).json()
    assert (b["status"], b["cancelReason"], b["cancellationFee"]) == ("cancelled", "customer", None)
    assert b["refund"] == {"amount": {"amount": 300, "currency": "KES"}, "status": "due", "refundedAt": None, "reference": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("deposit,fee,refund", [(300, 300, None), (500, 400, 100)])
async def test_late_cancellation_keeps_a_fee(client, auth_headers, salon, customer, db_session, deposit, fee, refund):
    _, cust = customer
    booking_id = await paid_booking(client, auth_headers, salon, cust, db_session, deposit=deposit, free_hours=24 * 30)
    p = (await preview(client, cust, booking_id)).json()
    assert (p["withinFreeWindow"], p["fee"]["amount"], p["lateFeePercent"]) == (False, fee, 50)
    assert p["message"].startswith("Late cancellation")

    b = (await cancel(client, cust, booking_id)).json()
    assert b["cancellationFee"] == {"amount": fee, "currency": "KES"}
    assert (b["refund"]["amount"]["amount"] if b["refund"] else None) == refund


@pytest.mark.asyncio
async def test_unpaid_hold_cancels_for_free(client, auth_headers, salon, customer):
    _, cust = customer
    await set_deposit(client, auth_headers, salon, {"required": True, "type": "fixed", "amount": {"amount": 300, "currency": "KES"}, "appliesTo": "all_services"})
    booking_id = (await book(client, cust, body(salon))).json()["bookingId"]
    assert (await preview(client, cust, booking_id)).json()["message"] == "Free to cancel: no deposit was paid."
    b = (await cancel(client, cust, booking_id)).json()
    assert (b["status"], b["refund"], b["cancellationFee"]) == ("cancelled", None, None)


@pytest.mark.asyncio
async def test_cannot_cancel_twice_or_someone_elses(client, salon, customer, db_session):
    _, cust = customer
    booking_id = (await book(client, cust, body(salon))).json()["bookingId"]
    stranger = await make_user(db_session, "not.mine@example.com")
    assert (await cancel(client, headers_for(stranger), booking_id)).status_code == 404
    assert (await cancel(client, cust, booking_id)).status_code == 200
    again = await cancel(client, cust, booking_id)
    assert again.status_code == 409 and again.json()["error"] == "cannot_cancel"


# ─── Business cancels, then refunds by hand ──────────────────


@pytest.mark.asyncio
async def test_business_cancel_and_mark_refunded(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    booking_id = await paid_booking(client, auth_headers, salon, cust, db_session, free_hours=24 * 30)  # "late", but it's their call
    b = (await client.patch(f"/api/v1/bookings/{booking_id}/status", json={"status": "cancelled"}, headers=auth_headers)).json()
    assert (b["cancelReason"], b["refund"]["amount"]["amount"], b["refund"]["status"]) == ("business", 300, "due")

    owed = await client.get(f"/api/v1/businesses/{salon['id']}/bookings", params={"refund": "due"}, headers=auth_headers)
    assert [x["bookingId"] for x in owed.json()["bookings"]] == [booking_id]

    assert (await client.post(f"/api/v1/bookings/{booking_id}/refund", json={}, headers=cust)).status_code == 404  # not the customer
    done = (await client.post(f"/api/v1/bookings/{booking_id}/refund", json={"reference": "RFD12345"}, headers=auth_headers)).json()
    assert (done["refund"]["status"], done["refund"]["reference"]) == ("refunded", "RFD12345")
    assert done["refund"]["refundedAt"] is not None
    assert (await client.post(f"/api/v1/bookings/{booking_id}/refund", json={}, headers=auth_headers)).status_code == 409

    owed = await client.get(f"/api/v1/businesses/{salon['id']}/bookings", params={"refund": "due"}, headers=auth_headers)
    assert owed.json()["bookings"] == []
    # The customer sees it went through.
    assert (await client.get(f"/api/v1/bookings/{booking_id}", headers=cust)).json()["refund"]["status"] == "refunded"
