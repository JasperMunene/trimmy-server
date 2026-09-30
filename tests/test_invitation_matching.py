import uuid

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.modules.auth.models import AuthType, User
from app.modules.business.models import TeamInvitation
from tests.test_staff_identity import _business


async def person(db_session, email, phone=None, auth_type=AuthType.EMAIL):
    user = User(id=uuid.uuid4(), email=email, phone=phone, first_name="Faith", auth_type=auth_type, verified=True)
    db_session.add(user)
    await db_session.commit()
    return {"Authorization": f"Bearer {create_access_token(data={'sub': str(user.id), 'email': user.email})}"}


async def invite(client, owner, biz_id, **contact):
    resp = await client.post(f"/api/v1/businesses/{biz_id}/team/members",
                             json={"name": "Faith", "role": "front_desk", **contact}, headers=owner)
    assert resp.status_code == 201, resp.text
    return resp.json()["invitationId"]


async def mine(client, headers):
    return [i["invitationId"] for i in (await client.get("/api/v1/me/invitations", headers=headers)).json()["invitations"]]


@pytest.mark.asyncio
async def test_email_invite_reaches_a_google_user_without_a_phone(client, auth_headers, db_session):
    biz_id = await _business(client, auth_headers)
    inv_id = await invite(client, auth_headers, biz_id, email="Faith.K@Example.com ")
    google = await person(db_session, "faith.k@example.com", auth_type=AuthType.GOOGLE)
    assert await mine(client, google) == [inv_id]
    resp = await client.post(f"/api/v1/invitations/{inv_id}/respond", json={"action": "accept"}, headers=google)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_phone_matches_in_any_format(client, auth_headers, db_session):
    biz_id = await _business(client, auth_headers)
    inv_id = await invite(client, auth_headers, biz_id, phone="0722 000 111")
    stored = (await db_session.execute(select(TeamInvitation.phone).where(TeamInvitation.id == uuid.UUID(inv_id)))).scalar_one()
    assert stored == "+254722000111"
    assert await mine(client, await person(db_session, "p1@example.com", phone="+254722000111")) == [inv_id]


@pytest.mark.asyncio
async def test_duplicates_are_caught_across_formats(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    await invite(client, auth_headers, biz_id, phone="0722000111", email="a@example.com")
    for contact in ({"phone": "+254 722 000 111"}, {"email": "A@EXAMPLE.COM"}):
        resp = await client.post(f"/api/v1/businesses/{biz_id}/team/members",
                                 json={"name": "Again", "role": "staff", **contact}, headers=auth_headers)
        assert resp.status_code == 409 and resp.json()["error"] == "already_invited"


@pytest.mark.asyncio
async def test_nobody_else_sees_or_accepts_it(client, auth_headers, db_session):
    biz_id = await _business(client, auth_headers)
    inv_id = await invite(client, auth_headers, biz_id, email="faith@example.com", phone="0722000111")
    other = await person(db_session, "someone@example.com", phone="+254733999888")
    assert await mine(client, other) == []
    resp = await client.post(f"/api/v1/invitations/{inv_id}/respond", json={"action": "accept"}, headers=other)
    assert resp.status_code == 404
