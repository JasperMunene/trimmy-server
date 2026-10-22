import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import app.modules.notifications.tasks as notification_tasks
from app.core.security import create_access_token
from app.modules.auth.models import AuthType, User
from app.modules.booking.customer_match import claim_bookings
from app.modules.booking.models import Booking
from app.modules.business.models import Staff
from app.modules.payouts.models import WalletAdjustment
from tests.test_business_bookings import owner_body
from tests.test_charge import charge, checkout_id, get, till  # noqa: F401  (fixtures)
from tests.test_create_booking import day, salon  # noqa: F401  (fixtures)
from tests.test_mpesa_callback import callback, callback_secret, post_callback  # noqa: F401  (fixtures)
from tests.test_payments import provider  # noqa: F401  (fixtures)

PHONE = "+254711222333"


async def account(db, *, phone=None, email=None, auth_type=AuthType.PHONE, verified=True, phone_proven=True):
    user = User(id=uuid.uuid4(), phone=phone, email=email, first_name="Amina", auth_type=auth_type, verified=verified,
                verified_at=datetime.now(timezone.utc) if verified else None,
                phone_verified_at=datetime.now(timezone.utc) if phone and phone_proven else None)
    db.add(user)
    await db.commit()
    return user


def headers(user):
    return {"Authorization": f"Bearer {create_access_token(data={'sub': str(user.id)})}"}


async def scheduled(client, auth_headers, salon, **extra):
    r = await client.post("/api/v1/bookings/scheduled",
                          json=owner_body(salon, staff="a", date=day().isoformat(), time="11:00", customerName="Amina", **extra),
                          headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()


# ─── Matching business-made bookings to accounts ─────────────


@pytest.mark.asyncio
async def test_a_proven_phone_links_the_booking(client, auth_headers, salon, db_session):
    user = await account(db_session, phone=PHONE)
    b = await scheduled(client, auth_headers, salon, customerPhone="0711222333")  # any Kenyan format
    assert b["customerHasAccount"] is True
    mine = (await client.get("/api/v1/me/bookings", params={"filter": "upcoming"}, headers=headers(user))).json()
    assert [x["bookingId"] for x in mine["bookings"]] == [b["bookingId"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["unproven_phone", "unverified_email", "phone_signup_email"])
async def test_unproven_contacts_do_not_link(client, auth_headers, salon, db_session, who):
    if who == "unproven_phone":  # e.g. typed into a profile
        await account(db_session, phone=PHONE, auth_type=AuthType.EMAIL, email="a@example.com", phone_proven=False)
        b = await scheduled(client, auth_headers, salon, customerPhone=PHONE)
    elif who == "unverified_email":
        await account(db_session, email="unverified@example.com", auth_type=AuthType.EMAIL, verified=False)
        b = await scheduled(client, auth_headers, salon, customerEmail="unverified@example.com")
    else:  # an email given at phone signup was never proven
        await account(db_session, phone="+254700999111", email="side@example.com", auth_type=AuthType.PHONE)
        b = await scheduled(client, auth_headers, salon, customerEmail="side@example.com")
    assert b["customerHasAccount"] is False


@pytest.mark.asyncio
async def test_a_verified_email_links_case_insensitively(client, auth_headers, salon, db_session):
    await account(db_session, email="Amina@Example.com", auth_type=AuthType.EMAIL)
    assert (await scheduled(client, auth_headers, salon, customerEmail="amina@example.com"))["customerHasAccount"] is True


@pytest.mark.asyncio
async def test_signing_up_later_claims_recent_bookings(client, auth_headers, salon, db_session):
    b = await scheduled(client, auth_headers, salon, customerPhone=PHONE)
    assert b["customerHasAccount"] is False
    user = await account(db_session, phone=PHONE)
    assert await claim_bookings(db_session, user) == 1
    await db_session.commit()
    row = await db_session.get(Booking, uuid.UUID(b["bookingId"]))
    await db_session.refresh(row)
    assert row.customer_id == user.id


@pytest.mark.asyncio
async def test_changing_phone_unproves_it(client, db_session):
    user = await account(db_session, phone=PHONE)
    r = await client.patch("/api/v1/auth/me", json={"phone": "+254722000999"}, headers=headers(user))
    assert r.status_code == 200, r.text
    await db_session.refresh(user)
    assert user.phone_verified_at is None


# ─── Tips at checkout ────────────────────────────────────────


@pytest.fixture
async def stylist_user(db_session, salon):
    user = User(id=uuid.uuid4(), email="tipped.stylist@example.com", auth_type=AuthType.EMAIL, verified=True)
    db_session.add(user)
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id = user.id
    await db_session.commit()
    return user


@pytest.fixture
def told(monkeypatch):
    calls = []

    async def record(user_id, title, body, **data):
        calls.append((user_id, title, body, data.get("type")))

    monkeypatch.setattr(notification_tasks, "notify", record)
    return calls


async def walk_in_with(client, auth_headers, salon, staff="a"):
    r = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff=staff, customerName="Amina",
                                                                     customerPhone="+254733000111"), headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["bookingId"]


async def tips_in_wallet(db_session, booking_id):
    db_session.expire_all()
    return (await db_session.execute(
        select(WalletAdjustment.amount).where(WalletAdjustment.booking_id == uuid.UUID(booking_id), WalletAdjustment.kind == "tip")
    )).scalars().all()


@pytest.mark.asyncio
async def test_cash_with_tip(client, auth_headers, salon, db_session, stylist_user, told):
    import asyncio

    stylist_id = stylist_user.id
    booking_id = await walk_in_with(client, auth_headers, salon)
    r = await charge(client, auth_headers, booking_id, method="cash", tip=200)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["tip"] == {"amount": 200, "currency": "KES"} and b["paymentStatus"] == "paid"
    assert b["totalAmount"]["amount"] == 800  # the sale; the tip isn't revenue
    assert await tips_in_wallet(db_session, booking_id) == [200]
    await asyncio.sleep(0.01)  # the after-commit notification
    assert told and told[-1][0] == stylist_id and "KES 200" in told[-1][2] and told[-1][3] == "wallet_adjusted"

    # Accounting: the drawer holds 1000, of which 200 is the stylist's; revenue stays 800.
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Africa/Nairobi")).date().isoformat()
    summary = (await client.get(f"/api/v1/businesses/{salon['id']}/day-summary", params={"date": today},
                                headers=auth_headers)).json()
    assert summary["collected"]["cash"]["amount"] == 1000 and summary["collected"]["tips"] == {"amount": 200, "count": 1}
    wallet = (await client.get(f"/api/v1/businesses/{salon['id']}/staff/{salon['a']}/payout-balance", headers=auth_headers)).json()
    assert wallet["tips"] == 200


@pytest.mark.asyncio
async def test_mpesa_with_tip_credits_on_success_only(client, auth_headers, salon, db_session, till, provider, stylist_user, told):
    booking_id = await walk_in_with(client, auth_headers, salon)
    started = (await charge(client, auth_headers, booking_id, method="mpesa", tip=150)).json()
    assert started["amount"] == 950 and provider.calls[-1]["amount"] == 950  # one prompt for both
    assert await tips_in_wallet(db_session, booking_id) == []  # nothing until it's paid

    await post_callback(client, callback(await checkout_id(db_session, started["paymentId"]), receipt="TIP12345XY", amount=950))
    done = await get(client, auth_headers, booking_id)
    assert done["tip"]["amount"] == 150 and done["paymentStatus"] == "paid"
    assert await tips_in_wallet(db_session, booking_id) == [150]


@pytest.mark.asyncio
async def test_a_tip_needs_someone_to_go_to(client, auth_headers, salon):
    r = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon), headers=auth_headers)  # unassigned
    resp = await charge(client, auth_headers, r.json()["bookingId"], method="cash", tip=100)
    assert resp.status_code == 400 and resp.json()["error"] == "tip_needs_staff"


# ─── Earnings count paid visits on the day they were paid ────


@pytest.mark.asyncio
async def test_future_booking_done_early_counts_today(client, auth_headers, salon, db_session):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    later = (datetime.now(ZoneInfo("Africa/Nairobi")) + timedelta(days=3)).date()
    r = await client.post("/api/v1/bookings/scheduled",
                          json=owner_body(salon, staff="a", date=later.isoformat(), time="11:00", customerName="Early"),
                          headers=auth_headers)
    booking_id = r.json()["bookingId"]
    assert (await client.patch(f"/api/v1/bookings/{booking_id}/status", json={"status": "in_progress"},
                               headers=auth_headers)).status_code == 200
    assert (await charge(client, auth_headers, booking_id, method="cash")).status_code == 200

    today = datetime.now(ZoneInfo("Africa/Nairobi")).date().isoformat()
    q = {"from": today, "to": today}
    business = (await client.get(f"/api/v1/businesses/{salon['id']}/earnings", params=q, headers=auth_headers)).json()
    assert business["totalRevenue"] == 800  # on the day the money came in, not the day it was booked for
    staff = (await client.get(f"/api/v1/businesses/{salon['id']}/earnings", params={**q, "staffId": salon["a"]},
                              headers=auth_headers)).json()
    assert staff["totalRevenue"] == 800
    on_booked_day = (await client.get(f"/api/v1/businesses/{salon['id']}/earnings",
                                      params={"from": later.isoformat(), "to": later.isoformat()}, headers=auth_headers)).json()
    assert on_booked_day["totalRevenue"] == 0  # not counted twice
