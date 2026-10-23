import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import create_access_token
from app.modules.auth.models import AuthType, User
from app.modules.booking.models import Booking
from app.modules.booking.states import record_created, transition
from app.modules.business.models import Staff
from tests.test_create_booking import NBO, body, book, customer, day, salon  # noqa: F401  (fixtures)


PATHS = {
    "pending_payment": [], "confirmed": [], "in_progress": ["in_progress"],
    "completed": ["in_progress", "completed"], "no_show": ["no_show"], "cancelled": ["cancelled"],
}


def headers_for(user):
    return {"Authorization": f"Bearer {create_access_token(data={'sub': str(user.id), 'email': user.email})}"}


async def make_user(db_session, email, phone=None):
    user = User(id=uuid.uuid4(), email=email, phone=phone, auth_type=AuthType.EMAIL, verified=True, first_name=email.split("@")[0])
    db_session.add(user)
    await db_session.commit()
    return user


async def insert_booking(db_session, salon, customer_id, starts_at, status="confirmed", **extra):
    b = Booking(
        business_id=uuid.UUID(salon["id"]), staff_id=uuid.UUID(salon["a"]), customer_id=customer_id,
        starts_at=starts_at, ends_at=starts_at + timedelta(minutes=45),
        local_date=starts_at.astimezone(NBO).date(), local_time=starts_at.astimezone(NBO).strftime("%H:%M"),
        duration_minutes=45, total_amount=800, customer_name="Amina", **extra,
    )
    # Walk the state machine to the wanted status, like the real flows do.
    record_created(b, "pending_payment" if status == "pending_payment" else "confirmed")
    for step in PATHS[status]:
        transition(b, step)
    db_session.add(b)
    await db_session.commit()
    return b


# ─── GET /bookings/{id} ──────────────────────────────────────


@pytest.mark.asyncio
async def test_who_can_see_a_booking(client, auth_headers, salon, customer, db_session):
    _, cust_headers = customer
    booking_id = (await book(client, cust_headers, body(salon))).json()["bookingId"]
    url = f"/api/v1/bookings/{booking_id}"

    assert (await client.get(url, headers=cust_headers)).json()["bookingId"] == booking_id  # the customer
    assert (await client.get(url, headers=auth_headers)).status_code == 200  # the owner

    worker = await make_user(db_session, "staffer@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["b"]))
    member.user_id = worker.id
    await db_session.commit()
    assert (await client.get(url, headers=headers_for(worker))).status_code == 200  # staff there

    stranger = await make_user(db_session, "stranger@example.com")
    assert (await client.get(url, headers=headers_for(stranger))).status_code == 404
    assert (await client.get("/api/v1/bookings/not-a-uuid", headers=cust_headers)).status_code == 404
    assert (await client.get(url)).status_code == 401


@pytest.mark.asyncio
async def test_get_shows_an_expired_hold_as_cancelled(client, salon, customer, db_session):
    user, cust_headers = customer
    start = datetime.now(timezone.utc) + timedelta(days=3)
    held = await insert_booking(
        db_session, salon, user.id, start, status="pending_payment",
        deposit_amount=200, expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    data = (await client.get(f"/api/v1/bookings/{held.id}", headers=cust_headers)).json()
    assert (data["status"], data["cancelReason"], data["expiresAt"]) == ("cancelled", "payment_expired", None)
    assert data["depositPayment"] is None


# ─── GET /me/bookings ────────────────────────────────────────


@pytest.fixture
async def history(salon, customer, db_session):
    user, _ = customer
    now = datetime.now(timezone.utc)
    rows = {
        "soon": await insert_booking(db_session, salon, user.id, now + timedelta(days=1)),
        "later": await insert_booking(db_session, salon, user.id, now + timedelta(days=5)),
        "held": await insert_booking(db_session, salon, user.id, now + timedelta(days=2), status="pending_payment",
                                     deposit_amount=200, expires_at=now + timedelta(minutes=5)),
        "done": await insert_booking(db_session, salon, user.id, now - timedelta(days=3), status="completed"),
        "missed": await insert_booking(db_session, salon, user.id, now - timedelta(days=1), status="no_show"),
        "stale": await insert_booking(db_session, salon, user.id, now - timedelta(days=2)),  # confirmed but over
        "called_off": await insert_booking(db_session, salon, user.id, now + timedelta(days=4), status="cancelled"),
    }
    other = await make_user(db_session, "someone@example.com")
    await insert_booking(db_session, salon, other.id, now + timedelta(days=1, hours=2))  # not mine
    return {k: str(v.id) for k, v in rows.items()}


async def ids(client, headers, **params):
    resp = await client.get("/api/v1/me/bookings", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return [b["bookingId"] for b in resp.json()["bookings"]]


@pytest.mark.asyncio
async def test_filters(client, customer, history):
    _, h = customer
    assert await ids(client, h) == [history["soon"], history["held"], history["later"]]  # upcoming, soonest first
    assert await ids(client, h, filter="past") == [history["missed"], history["stale"], history["done"]]  # newest first
    assert await ids(client, h, filter="cancelled") == [history["called_off"]]


@pytest.mark.asyncio
async def test_pagination(client, customer, history):
    _, h = customer
    seen, cursor = [], None
    for _ in range(5):
        params = {"filter": "upcoming", "limit": 1, **({"cursor": cursor} if cursor else {})}
        data = (await client.get("/api/v1/me/bookings", params=params, headers=h)).json()
        seen += [b["bookingId"] for b in data["bookings"]]
        cursor = data["nextCursor"]
        if cursor is None:
            break
    assert seen == [history["soon"], history["held"], history["later"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"filter": "all"}, {"limit": 0}, {"limit": 51}, {"cursor": "not-a-cursor"}])
async def test_list_validation(client, customer, params):
    _, h = customer
    resp = await client.get("/api/v1/me/bookings", params=params, headers=h)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_requires_sign_in(client):
    assert (await client.get("/api/v1/me/bookings")).status_code == 401
