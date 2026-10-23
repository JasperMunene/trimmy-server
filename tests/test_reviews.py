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


# ─── Review lists, detail and replies ────────────────────────


@pytest.mark.asyncio
async def test_review_carries_reviewer_staff_service_and_date(client, salon, customer, db_session):
    user, h = customer
    user.avatar_url = "https://media.example/u.jpg"
    await db_session.commit()
    booking_id = await visit(db_session, salon, user.id)
    await review(client, h, booking_id)

    page = (await client.get(f"/api/v1/businesses/{salon['id']}/reviews")).json()
    assert page["total"] == 1 and page["averageRating"] == 5.0
    (r,) = page["reviews"]
    assert r["authorAvatarUrl"] == "https://media.example/u.jpg"
    assert r["staffId"] == salon["a"] and r["staffName"] == "Wanjiku"
    assert r["bookingId"] == booking_id and r["visitDate"]
    assert r["reply"] is None


@pytest.mark.asyncio
async def test_filter_by_staff_and_my_reviews(client, salon, customer, db_session):
    user, h = customer
    await review(client, h, await visit(db_session, salon, user.id), rating=4)
    other = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) - timedelta(hours=5), status="completed")
    other.staff_id = uuid.UUID(salon["b"])
    await db_session.commit()
    await review(client, h, str(other.id), rating=2)

    by_b = (await client.get(f"/api/v1/businesses/{salon['id']}/reviews", params={"staffId": salon["b"]})).json()
    assert [x["rating"] for x in by_b["reviews"]] == [2]
    mine = (await client.get("/api/v1/me/reviews", headers=h)).json()
    assert mine["total"] == 2 and {x["businessName"] for x in mine["reviews"]} == {"Glow Beauty"}


@pytest.mark.asyncio
async def test_business_replies_and_customer_sees_it(client, auth_headers, salon, customer, db_session):
    user, h = customer
    booking_id = await visit(db_session, salon, user.id)
    review_id = (await review(client, h, booking_id)).json()["reviewId"]

    todo = (await client.get(f"/api/v1/businesses/{salon['id']}/reviews", params={"unreplied": "true"})).json()
    assert [x["reviewId"] for x in todo["reviews"]] == [review_id]

    replied = await client.put(f"/api/v1/reviews/{review_id}/reply", json={"text": "Thanks, see you soon!"}, headers=auth_headers)
    assert replied.status_code == 200, replied.text
    assert replied.json()["reply"]["text"] == "Thanks, see you soon!"
    assert (await client.get(f"/api/v1/businesses/{salon['id']}/reviews", params={"unreplied": "true"})).json()["total"] == 0
    # On the customer's booking and their reviews list too.
    assert (await client.get(f"/api/v1/bookings/{booking_id}", headers=h)).json()["review"]["replyText"] == "Thanks, see you soon!"
    assert (await client.get("/api/v1/me/reviews", headers=h)).json()["reviews"][0]["reply"]["text"] == "Thanks, see you soon!"

    removed = await client.delete(f"/api/v1/reviews/{review_id}/reply", headers=auth_headers)
    assert removed.status_code == 200 and removed.json()["reply"] is None


@pytest.mark.asyncio
async def test_only_the_business_can_reply(client, salon, customer, db_session):
    user, h = customer
    review_id = (await review(client, h, await visit(db_session, salon, user.id))).json()["reviewId"]
    assert (await client.put(f"/api/v1/reviews/{review_id}/reply", json={"text": "me"}, headers=h)).status_code == 404

    stylist = await make_user(db_session, "stylist.reply@example.com")
    db_session.add(Staff(business_id=uuid.UUID(salon["id"]), user_id=stylist.id, name="Imani", role="staff"))
    await db_session.commit()
    assert (await client.put(f"/api/v1/reviews/{review_id}/reply", json={"text": "hi"}, headers=headers_for(stylist))).status_code == 403

    desk = await make_user(db_session, "desk.reply@example.com")
    db_session.add(Staff(business_id=uuid.UUID(salon["id"]), user_id=desk.id, name="Faith", role="front_desk", front_desk_access=True))
    await db_session.commit()
    assert (await client.put(f"/api/v1/reviews/{review_id}/reply", json={"text": "Thank you!"}, headers=headers_for(desk))).status_code == 200


@pytest.mark.asyncio
async def test_profile_shows_the_latest_six(client, salon, customer, db_session):
    user, h = customer
    for i in range(8):
        b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) - timedelta(days=i + 1), status="completed")
        await review(client, h, str(b.id), rating=5)
    profile = (await client.get(f"/api/v1/businesses/{salon['id']}/profile")).json()
    assert len(profile["reviews"]) == 6 and profile["reviewCount"] == 8
    assert (await client.get(f"/api/v1/businesses/{salon['id']}/reviews", params={"limit": 50})).json()["total"] == 8
