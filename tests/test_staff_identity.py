import uuid

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.modules.auth.models import AuthType, User
from app.modules.business.models import Business, Staff

MEMBER_PHONE = "+254711223344"


@pytest.fixture
async def member(db_session):
    user = User(
        id=uuid.uuid4(), email="wanjiku@example.com", phone=MEMBER_PHONE, first_name="Wanjiku",
        last_name="M", auth_type=AuthType.EMAIL, verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    token = create_access_token(data={"sub": str(user.id), "email": user.email})
    return user, {"Authorization": f"Bearer {token}"}


async def _business(client, headers):
    resp = await client.post(
        "/api/v1/businesses",
        json={"name": "Glow", "categories": ["hair_salon"], "phone": "+254700998877",
              "location": {"address": "Kilimani", "lat": -1.29, "lng": 36.78}},
        headers=headers,
    )
    return resp.json()["businessId"]


async def _publish(db_session, biz_id):
    # Profile only lists published businesses; skip the full onboarding here.
    business = await db_session.get(Business, uuid.UUID(biz_id))
    business.status = "published"
    await db_session.commit()


async def _invite_and_accept(client, owner_headers, member_headers, biz_id, **member):
    body = {"name": "Wanjiku", "phone": MEMBER_PHONE, "role": "staff", "commissionPercent": 45,
            "workingDays": ["friday", "monday"], **member}
    added = await client.post(f"/api/v1/businesses/{biz_id}/team/members", json=body, headers=owner_headers)
    assert added.status_code == 201, added.text
    assert added.json()["staffId"] is None  # not bookable until accepted
    inv_id = added.json()["invitationId"]
    resp = await client.post(f"/api/v1/invitations/{inv_id}/respond", json={"action": "accept"}, headers=member_headers)
    assert resp.status_code == 200, resp.text
    return inv_id


@pytest.mark.asyncio
async def test_accept_makes_one_staff_id_everywhere(client, auth_headers, member, db_session):
    user, member_headers = member
    biz_id = await _business(client, auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id)

    (roster,) = (await client.get(f"/api/v1/businesses/{biz_id}/team/members", headers=auth_headers)).json()["members"]
    staff_id = roster["staffId"]
    assert staff_id is not None

    staff = await db_session.get(Staff, uuid.UUID(staff_id))
    assert staff.user_id == user.id
    assert staff.working_days == ["monday", "friday"]
    assert staff.commission_percent == 45

    await _publish(db_session, biz_id)
    profile = (await client.get(f"/api/v1/businesses/{biz_id}/profile")).json()
    (listed,) = [s for s in profile["staff"] if s["staffId"] == staff_id]
    assert listed["workingDays"] == ["monday", "friday"]
    assert "commissionPercent" not in listed  # private to the owner


@pytest.mark.asyncio
async def test_edits_after_accept_update_staff(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id = await _business(client, auth_headers)
    inv_id = await _invite_and_accept(client, auth_headers, member_headers, biz_id)

    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/team/members/{inv_id}",
        json={"commissionPercent": 50, "workingDays": ["saturday"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    staff = await db_session.get(Staff, uuid.UUID(resp.json()["staffId"]))
    await db_session.refresh(staff)
    assert (staff.commission_percent, staff.working_days) == (50, ["saturday"])


@pytest.mark.asyncio
async def test_remove_after_accept_deletes_the_staff_row(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id = await _business(client, auth_headers)
    inv_id = await _invite_and_accept(client, auth_headers, member_headers, biz_id)

    assert (await client.delete(f"/api/v1/businesses/{biz_id}/team/members/{inv_id}", headers=auth_headers)).status_code == 204
    db_session.expire_all()
    assert (await db_session.execute(select(Staff).where(Staff.business_id == uuid.UUID(biz_id)))).first() is None


@pytest.mark.asyncio
async def test_null_working_days_means_business_hours(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id = await _business(client, auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id, workingDays=None)
    (roster,) = (await client.get(f"/api/v1/businesses/{biz_id}/team/members", headers=auth_headers)).json()["members"]
    staff = await db_session.get(Staff, uuid.UUID(roster["staffId"]))
    assert staff.working_days is None
