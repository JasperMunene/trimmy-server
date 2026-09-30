import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.business.models import Staff
from tests.test_create_booking import customer, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, insert_booking, make_user


async def visit(db_session, salon, user_id, status="completed"):
    b = await insert_booking(db_session, salon, user_id, datetime.now(timezone.utc) - timedelta(hours=2), status=status)
    return str(b.id)


async def review(client, headers, booking_id, rating=5, text="Great fade"):
    return await client.post(f"/api/v1/bookings/{booking_id}/review", json={"rating": rating, "text": text}, headers=headers)


@pytest.mark.asyncio
async def test_review_a_completed_visit(client, salon, customer, db_session):
    user, h = customer
    booking_id = await visit(db_session, salon, user.id)
    assert (await client.get(f"/api/v1/bookings/{booking_id}", headers=h)).json()["review"] is None

    resp = await review(client, h, booking_id)
    assert resp.status_code == 201, resp.text
    r = resp.json()
    assert (r["rating"], r["text"]) == (5, "Great fade") and r["reviewId"] and r["createdAt"]
    assert r["authorName"]

    assert (await client.get(f"/api/v1/bookings/{booking_id}", headers=h)).json()["review"]["reviewId"] == r["reviewId"]
    profile = (await client.get(f"/api/v1/businesses/{salon['id']}/profile")).json()
    assert any(x.get("text") == "Great fade" for x in profile["reviews"])


@pytest.mark.asyncio
async def test_staff_rating_is_the_average(client, salon, customer, db_session):
    user, h = customer
    await review(client, h, await visit(db_session, salon, user.id), rating=5)
    await review(client, h, await visit(db_session, salon, user.id), rating=4)
    db_session.expire_all()
    assert (await db_session.get(Staff, uuid.UUID(salon["a"]))).rating == 4.5


@pytest.mark.asyncio
async def test_once_per_booking(client, salon, customer, db_session):
    user, h = customer
    booking_id = await visit(db_session, salon, user.id)
    assert (await review(client, h, booking_id)).status_code == 201
    again = await review(client, h, booking_id, rating=1)
    assert again.status_code == 409 and again.json()["error"] == "already_reviewed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["confirmed", "in_progress", "cancelled", "no_show"])
async def test_only_completed(client, salon, customer, db_session, status):
    user, h = customer
    resp = await review(client, h, await visit(db_session, salon, user.id, status=status))
    assert resp.status_code == 409 and resp.json()["error"] == "not_completed"


@pytest.mark.asyncio
async def test_only_the_customer_and_valid_ratings(client, auth_headers, salon, customer, db_session):
    user, h = customer
    booking_id = await visit(db_session, salon, user.id)
    stranger = await make_user(db_session, "not.my.visit@example.com")
    assert (await review(client, headers_for(stranger), booking_id)).status_code == 404
    assert (await review(client, auth_headers, booking_id)).status_code == 404  # the owner can't review for them
    assert (await review(client, h, str(uuid.uuid4()))).status_code == 404
    for bad in (0, 6):
        assert (await review(client, h, booking_id, rating=bad)).status_code == 422
    ok = await client.post(f"/api/v1/bookings/{booking_id}/review", json={"rating": 3}, headers=h)
    assert ok.status_code == 201 and ok.json()["text"] is None
