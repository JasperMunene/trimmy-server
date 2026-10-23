"""The discovery read cache (app/core/cache.py) and what clears it (cache_invalidation.py)."""
import uuid

import pytest
from sqlalchemy import text

from app.core import cache, cache_invalidation
from app.core.config import settings
from app.modules.auth.models import User
from app.modules.business.models import Review, Staff
from tests.test_discovery import publish_business


async def profile(client, business_id):
    resp = await client.get(f"/api/v1/businesses/{business_id}/profile")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def card(client, business_id):
    found = (await client.get("/api/v1/businesses/search")).json()["businesses"]
    return next(b for b in found if b["businessId"] == business_id)


@pytest.mark.asyncio
async def test_profile_and_search_are_served_from_the_cache(client, auth_headers, db_session):
    business_id = await publish_business(client, auth_headers, name="Cached Salon", phone="+254720000201", categories=["hair_salon"])
    await profile(client, business_id)
    await card(client, business_id)

    # A raw SQL change skips the ORM entirely, so nothing clears the cache: still the old name.
    bid = uuid.UUID(business_id)
    done = await db_session.execute(text("UPDATE businesses SET name = 'Renamed Behind Its Back' WHERE id IN (:a, :b)"),
                                    {"a": bid.hex, "b": str(bid)})
    assert done.rowcount == 1
    await db_session.commit()
    await cache_invalidation.settle()
    assert (await profile(client, business_id))["name"] == "Cached Salon"
    assert (await card(client, business_id))["name"] == "Cached Salon"

    # Once it's gone (here: the TTL backstop), the change shows.
    cache.clear_local()
    assert (await profile(client, business_id))["name"] == "Renamed Behind Its Back"


@pytest.mark.asyncio
async def test_an_edit_clears_that_business_at_once(client, auth_headers):
    business_id = await publish_business(client, auth_headers, name="Before", phone="+254720000202", categories=["hair_salon"])
    assert (await profile(client, business_id))["name"] == "Before"
    assert (await card(client, business_id))["name"] == "Before"

    resp = await client.patch(f"/api/v1/businesses/{business_id}", json={"name": "After", "categories": ["hair_salon"], "phone": "+254720000202",
              "location": {"address": "Nairobi", "lat": -1.2921, "lng": 36.7833}}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    await cache_invalidation.settle()
    assert (await profile(client, business_id))["name"] == "After"
    assert (await card(client, business_id))["name"] == "After"


@pytest.mark.asyncio
async def test_a_new_review_updates_rating_everywhere(client, auth_headers, db_session):
    business_id = await publish_business(client, auth_headers, name="Reviewed", phone="+254720000203", categories=["hair_salon"])
    assert (await profile(client, business_id))["reviewCount"] == 0
    assert (await card(client, business_id))["reviewCount"] == 0

    db_session.add(Review(business_id=uuid.UUID(business_id), author_name="Amina", rating=5, text="Lovely"))
    await db_session.commit()
    await cache_invalidation.settle()
    assert (await profile(client, business_id))["reviewCount"] == 1
    assert (await card(client, business_id))["rating"] == 5.0


@pytest.mark.asyncio
async def test_staff_changes_and_renames_clear_profiles(client, auth_headers, db_session, test_user):
    business_id = await publish_business(client, auth_headers, name="Team Salon", phone="+254720000204", categories=["hair_salon"])
    staff = (await profile(client, business_id))["staff"]
    assert staff

    # A staff row edited directly (e.g. their bio).
    member = await db_session.get(Staff, uuid.UUID(staff[0]["staffId"]))
    member.bio = "Twenty years of fades"
    await db_session.commit()
    await cache_invalidation.settle()
    assert (await profile(client, business_id))["staff"][0]["bio"] == "Twenty years of fades"

    # Renaming yourself syncs your name onto every team with a bulk UPDATE: every profile is retired.
    resp = await client.patch("/api/v1/auth/me", json={"first_name": "Renamed", "last_name": "Owner"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    await cache_invalidation.settle()
    names = [s["name"] for s in (await profile(client, business_id))["staff"]]
    assert "Renamed Owner" in names


@pytest.mark.asyncio
async def test_a_rolled_back_change_clears_nothing(client, auth_headers, db_session):
    business_id = await publish_business(client, auth_headers, name="Steady", phone="+254720000205", categories=["hair_salon"])
    await profile(client, business_id)
    db_session.add(Review(business_id=uuid.UUID(business_id), author_name="Ghost", rating=1, text="never saved"))
    await db_session.flush()
    await db_session.rollback()
    await cache_invalidation.settle()
    assert cache._local  # the cached profile and cards are still there


@pytest.mark.asyncio
async def test_redis_down_still_answers_from_the_database(client, auth_headers, monkeypatch):
    business_id = await publish_business(client, auth_headers, name="Offline Cache", phone="+254720000206", categories=["hair_salon"])
    monkeypatch.setattr(settings, "CACHE_BACKEND", "redis")
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens there
    monkeypatch.setattr(cache, "_redis", None)
    assert (await profile(client, business_id))["name"] == "Offline Cache"
    assert (await card(client, business_id))["name"] == "Offline Cache"
    monkeypatch.setattr(cache, "_redis", None)
