import pytest


@pytest.mark.asyncio
async def test_forgot_password_existing_user(client, test_user):
    response = await client.post("/api/v1/auth/forgot-password", json={
        "email": "test@example.com",
    })
    assert response.status_code == 200
    data = response.json()
    assert "message" in data


@pytest.mark.asyncio
async def test_forgot_password_nonexistent_user(client):
    response = await client.post("/api/v1/auth/forgot-password", json={
        "email": "nobody@example.com",
    })
    assert response.status_code == 200
    data = response.json()
    assert "message" in data


@pytest.mark.asyncio
async def test_forgot_password_invalid_email(client):
    response = await client.post("/api/v1/auth/forgot-password", json={
        "email": "not-an-email",
    })
    assert response.status_code == 422


# ─── Recovery by phone ───────────────────────────────────────

import uuid  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import app.modules.auth.service as auth_service  # noqa: E402
from app.core.security import verify_password  # noqa: E402
from app.modules.auth.models import AuthType, User  # noqa: E402


@pytest.fixture
def texts(monkeypatch):
    sent = []

    class FakeTask:
        async def enqueue(self, **kw):
            sent.append(kw)

    monkeypatch.setattr(auth_service, "send_otp_sms", FakeTask())
    return sent


async def phone_user(db_session, proven=True):
    user = User(id=uuid.uuid4(), phone="+254711000222", auth_type=AuthType.PHONE, verified=True,
                verified_at=datetime.now(timezone.utc),
                phone_verified_at=datetime.now(timezone.utc) if proven else None)
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.mark.asyncio
async def test_reset_by_phone_end_to_end(client, db_session, texts):
    user = await phone_user(db_session)
    r = await client.post("/api/v1/auth/forgot-password", json={"phone": "0711000222"})  # any Kenyan format
    assert r.status_code == 200 and texts and texts[0]["to"] == "+254711000222" and texts[0]["kind"] == "password_reset"
    otp = texts[0]["otp"]
    assert (await client.post("/api/v1/auth/verify-reset-otp", json={"phone": "+254711000222", "otp": otp})).json() == {"valid": True}
    done = await client.post("/api/v1/auth/reset-password",
                             json={"phone": "+254711000222", "otp": otp, "new_password": "brand-new-pass"})
    assert done.status_code == 200, done.text
    await db_session.refresh(user)
    assert verify_password("brand-new-pass", user.password_hash)


@pytest.mark.asyncio
async def test_unproven_phone_gets_no_code(client, db_session, texts):
    await phone_user(db_session, proven=False)
    r = await client.post("/api/v1/auth/forgot-password", json={"phone": "+254711000222"})
    assert r.status_code == 200 and texts == []  # same answer, nothing sent


@pytest.mark.asyncio
async def test_email_or_phone_not_both(client):
    assert (await client.post("/api/v1/auth/forgot-password", json={})).status_code == 422
    both = {"email": "a@example.com", "phone": "+254711000222"}
    assert (await client.post("/api/v1/auth/forgot-password", json=both)).status_code == 422
    assert (await client.post("/api/v1/auth/forgot-password", json={"phone": "12"})).status_code == 422
