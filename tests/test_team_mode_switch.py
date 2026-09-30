import uuid

import pytest
from sqlalchemy import select

from app.modules.business.models import Business, Staff
from tests.test_create_booking import salon  # noqa: F401  (fixture)


async def switch(client, headers, biz_id, mode):
    resp = await client.patch(f"/api/v1/businesses/{biz_id}/team-mode", json={"teamMode": mode}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def my_staff_id(client, headers, biz_id):
    (entry,) = [b for b in (await client.get("/api/v1/me/businesses", headers=headers)).json()["businesses"] if b["businessId"] == biz_id]
    return entry["staffId"]


@pytest.mark.asyncio
async def test_published_solo_to_team_keeps_step_and_owner(client, auth_headers, salon, db_session):
    business = await db_session.get(Business, uuid.UUID(salon["id"]))
    business.team_mode, business.onboarding_step = "solo", "review_publish"
    await db_session.commit()
    await switch(client, auth_headers, salon["id"], "solo")  # publishes-as-solo gives the owner a Staff row
    owner_staff = await my_staff_id(client, auth_headers, salon["id"])
    assert owner_staff is not None

    resp = await switch(client, auth_headers, salon["id"], "team")
    assert (resp["teamMode"], resp["onboardingStep"]) == ("team", "review_publish")
    assert await my_staff_id(client, auth_headers, salon["id"]) == owner_staff  # still bookable
    profile = (await client.get(f"/api/v1/businesses/{salon['id']}/profile")).json()
    assert owner_staff in [s["staffId"] for s in profile["staff"]]


@pytest.mark.asyncio
async def test_going_solo_again_does_not_duplicate_the_owner(client, auth_headers, salon, db_session):
    for mode in ("solo", "team", "solo"):
        await switch(client, auth_headers, salon["id"], mode)
    business = await db_session.get(Business, uuid.UUID(salon["id"]))
    rows = (await db_session.execute(select(Staff.id).where(Staff.business_id == business.id, Staff.user_id == business.owner_id))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_wizard_moves_forward_only(client, auth_headers):
    biz_id = (await client.post(
        "/api/v1/businesses",
        json={"name": "Fresh", "categories": ["hair_salon"], "phone": "+254700445566",
              "location": {"address": "Kilimani", "lat": -1.29, "lng": 36.78}},
        headers=auth_headers,
    )).json()["businessId"]
    assert (await switch(client, auth_headers, biz_id, "team"))["onboardingStep"] == "team_mode"
    assert await my_staff_id(client, auth_headers, biz_id) is None  # drafts get the owner row at publish
