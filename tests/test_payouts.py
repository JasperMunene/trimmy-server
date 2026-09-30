import uuid

import pytest

from app.modules.booking.models import Booking
from app.modules.business.models import Staff
from tests.test_business_bookings import owner_body
from tests.test_create_booking import customer, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


@pytest.fixture
async def stylist(salon, db_session):
    """Staff member A with a login and 40% commission."""
    user = await make_user(db_session, "wanjiku.payouts@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id, member.commission_percent = user.id, 40
    await db_session.commit()
    return headers_for(user)


async def paid_visit(client, auth_headers, salon, staff="a", services=(("cut", 1),)):
    """A walk-in for `staff`, paid in cash (cut = KES 800)."""
    booking = (await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff=staff, services=services), headers=auth_headers)).json()
    resp = await client.post(f"/api/v1/bookings/{booking['bookingId']}/charge", json={"method": "cash"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    return booking["bookingId"]


async def balance(client, headers, salon):
    resp = await client.get("/api/v1/me/payouts/balance", params={"businessId": salon["id"]}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def request(client, headers, salon, amount, **extra):
    return await client.post("/api/v1/payouts", json={"businessId": salon["id"], "amount": amount, **extra}, headers=headers)


@pytest.mark.asyncio
async def test_commission_is_fixed_when_paid(client, auth_headers, salon, stylist, db_session):
    booking_id = await paid_visit(client, auth_headers, salon)
    assert (await db_session.get(Booking, uuid.UUID(booking_id))).staff_commission == 320  # 40% of 800

    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.commission_percent = 50  # a raise afterwards doesn't rewrite past earnings
    await db_session.commit()
    b = await balance(client, stylist, salon)
    assert (b["earned"], b["available"], b["commissionPercent"], b["staffId"]) == (320, 320, 50, salon["a"])


@pytest.mark.asyncio
async def test_request_reject_and_approve(client, auth_headers, salon, stylist):
    await paid_visit(client, auth_headers, salon)

    first = await request(client, stylist, salon, 200)
    assert first.status_code == 201, first.text
    p = first.json()
    assert (p["status"], p["amount"], p["staffId"], p["staffName"]) == ("pending", 200, salon["a"], "Wanjiku")
    assert (await balance(client, stylist, salon))["available"] == 120

    over = await request(client, stylist, salon, 150)
    assert over.status_code == 409 and over.json()["error"] == "insufficient_balance"

    rejected = (await client.post(f"/api/v1/payouts/{p['payoutId']}/respond",
                                  json={"action": "reject", "rejectionReason": "Wait for month end"}, headers=auth_headers)).json()
    assert (rejected["status"], rejected["rejectionReason"]) == ("rejected", "Wait for month end")
    assert (await balance(client, stylist, salon))["available"] == 320  # a rejection frees the amount

    second = (await request(client, stylist, salon, 320)).json()
    approved = (await client.post(f"/api/v1/payouts/{second['payoutId']}/respond",
                                  json={"action": "approve", "rejectionReason": "ignored"}, headers=auth_headers)).json()
    assert (approved["status"], approved["rejectionReason"]) == ("paid", None)
    assert approved["respondedAt"] is not None
    b = await balance(client, stylist, salon)
    assert (b["earned"], b["paid"], b["pending"], b["available"]) == (320, 320, 0, 0)

    again = await client.post(f"/api/v1/payouts/{second['payoutId']}/respond", json={"action": "reject"}, headers=auth_headers)
    assert again.status_code == 409 and again.json()["error"] == "already_responded"


@pytest.mark.asyncio
async def test_lists(client, auth_headers, salon, stylist, db_session):
    await paid_visit(client, auth_headers, salon)
    await paid_visit(client, auth_headers, salon, staff="b")  # B has no commission rate: earns nothing
    mine = (await request(client, stylist, salon, 100)).json()

    owner_view = (await client.get(f"/api/v1/businesses/{salon['id']}/payouts", params={"status": "pending"}, headers=auth_headers)).json()
    assert [p["payoutId"] for p in owner_view["payouts"]] == [mine["payoutId"]]

    other = await make_user(db_session, "otieno.payouts@example.com")
    b = await db_session.get(Staff, uuid.UUID(salon["b"]))
    b.user_id = other.id
    await db_session.commit()
    their_view = (await client.get(f"/api/v1/businesses/{salon['id']}/payouts", headers=headers_for(other))).json()
    assert their_view["payouts"] == []  # staff only see their own
    assert (await balance(client, headers_for(other), salon))["earned"] == 0


@pytest.mark.asyncio
async def test_unassigned_bookings_earn_no_commission(client, auth_headers, salon, stylist):
    await paid_visit(client, auth_headers, salon, staff=None)
    assert (await balance(client, stylist, salon))["earned"] == 0


@pytest.mark.asyncio
async def test_permissions(client, auth_headers, salon, stylist, customer, db_session):
    _, cust = customer
    await paid_visit(client, auth_headers, salon)
    # Staff request only for themselves; customers aren't staff.
    assert (await request(client, stylist, salon, 10, staffId=salon["b"])).status_code == 403
    assert (await request(client, cust, salon, 10)).status_code == 404
    assert (await client.get("/api/v1/me/payouts/balance", params={"businessId": salon["id"]}, headers=cust)).status_code == 404

    # Balances: the owner can see anyone's; staff only their own.
    owner_sees = await client.get(f"/api/v1/businesses/{salon['id']}/staff/{salon['a']}/payout-balance", headers=auth_headers)
    assert owner_sees.status_code == 200 and owner_sees.json()["earned"] == 320
    other = await make_user(db_session, "peek@example.com")
    b = await db_session.get(Staff, uuid.UUID(salon["b"]))
    b.user_id = other.id
    await db_session.commit()
    peek = await client.get(f"/api/v1/businesses/{salon['id']}/staff/{salon['a']}/payout-balance", headers=headers_for(other))
    assert peek.status_code == 403

    # Only the owner answers.
    payout = (await request(client, stylist, salon, 100)).json()
    assert (await client.post(f"/api/v1/payouts/{payout['payoutId']}/respond", json={"action": "approve"}, headers=stylist)).status_code == 404
    assert (await request(client, stylist, salon, 0)).status_code == 422
