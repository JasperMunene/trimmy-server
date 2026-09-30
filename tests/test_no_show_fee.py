import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.booking.cancellation import no_show_outcome
from tests.test_create_booking import NBO, customer, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import insert_booking


@pytest.mark.parametrize("total,deposit,percent,expected", [
    (800, 300, 100, (800, 300, 500, 0)),   # the deposit covers part of the fee
    (800, 300, 25, (200, 200, 0, 100)),    # the fee is smaller: the rest of the deposit is refunded
    (800, 0, 100, (800, 0, 800, 0)),       # no deposit: nothing can be kept
    (800, 300, 0, (0, 0, 0, 300)),         # no fee: the deposit goes back
    (999, 500, 33, (330, 330, 0, 170)),    # rounds up to a whole shilling
])
def test_no_show_outcome(total, deposit, percent, expected):
    r = no_show_outcome(total=total, deposit_paid=deposit, fee_percent=percent)
    assert (r.fee, r.kept, r.uncollected, r.refund) == expected


async def missed(db_session, salon, user_id, deposit=0):
    b = await insert_booking(db_session, salon, user_id, datetime.now(timezone.utc) - timedelta(hours=1))
    if deposit:
        b.deposit_amount, b.payment_status = deposit, "deposit_paid"
    await db_session.commit()
    return str(b.id)


async def set_fee(client, auth_headers, salon, percent):
    await client.put(
        f"/api/v1/businesses/{salon['id']}/policies",
        json={"deposit": {"required": False, "type": "fixed", "appliesTo": "all_services"},
              "cancellation": {"freeCancellationHours": 24, "lateFeePercent": 50}, "noShow": {"feePercent": percent}},
        headers=auth_headers,
    )


async def mark_no_show(client, headers, booking_id):
    resp = await client.patch(f"/api/v1/bookings/{booking_id}/status", json={"status": "no_show"}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_fee_kept_from_the_deposit(client, auth_headers, salon, customer, db_session):
    user, cust = customer
    booking_id = await missed(db_session, salon, user.id, deposit=300)
    b = await mark_no_show(client, auth_headers, booking_id)  # default policy: 100%
    assert b["noShowFee"] == {
        "percent": 100,
        "fee": {"amount": 800, "currency": "KES"},
        "kept": {"amount": 300, "currency": "KES"},
        "uncollected": {"amount": 500, "currency": "KES"},
    }
    assert b["refund"] is None
    # The customer sees the same on their booking.
    assert (await client.get(f"/api/v1/bookings/{booking_id}", headers=cust)).json()["noShowFee"]["kept"]["amount"] == 300


@pytest.mark.asyncio
async def test_smaller_fee_refunds_the_rest(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    await set_fee(client, auth_headers, salon, 25)
    booking_id = await missed(db_session, salon, user.id, deposit=300)
    b = await mark_no_show(client, auth_headers, booking_id)
    assert (b["noShowFee"]["kept"]["amount"], b["refund"]["amount"]["amount"], b["refund"]["status"]) == (200, 100, "due")


@pytest.mark.asyncio
async def test_no_deposit_means_nothing_kept(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    b = await mark_no_show(client, auth_headers, await missed(db_session, salon, user.id))
    assert (b["noShowFee"]["kept"]["amount"], b["noShowFee"]["uncollected"]["amount"]) == (0, 800)


@pytest.mark.asyncio
async def test_other_bookings_have_no_fee(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(days=2))
    assert (await client.get(f"/api/v1/bookings/{b.id}", headers=auth_headers)).json()["noShowFee"] is None


@pytest.mark.asyncio
async def test_earnings_count_kept_no_show_fees(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    await mark_no_show(client, auth_headers, await missed(db_session, salon, user.id, deposit=300))
    today = datetime.now(NBO).date()
    yesterday = today - timedelta(days=1)
    e = (await client.get(f"/api/v1/businesses/{salon['id']}/earnings",
                          params={"from": yesterday.isoformat(), "to": today.isoformat()}, headers=auth_headers)).json()
    assert (e["noShow"], e["money"]["noShowFees"], e["totalRevenue"]) == (1, 300, 0)
