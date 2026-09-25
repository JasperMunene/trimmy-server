import hashlib

import pytest
from sqlalchemy import select

from app.modules.auth.models import AuthToken, User


@pytest.mark.asyncio
async def test_request_email_otp_creates_pending_user(client, db_session):
    email = "newbie@example.com"
    response = await client.post("/api/v1/auth/request-email-otp", json={"email": email})
    assert response.status_code == 200

    # A pending, password-less, unverified user now exists.
    user = (await db_session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    assert user is not None
    assert user.password_hash is None
    assert user.verified is False

    # And a verification OTP was stored for it.
    token = (
        await db_session.execute(
            select(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == "verification")
        )
    ).scalar_one_or_none()
    assert token is not None


@pytest.mark.asyncio
async def test_request_email_otp_rejects_verified_email(client, test_user):
    response = await client.post("/api/v1/auth/request-email-otp", json={"email": test_user.email})
    assert response.status_code == 409
    assert response.json()["error"] == "email_taken"


@pytest.mark.asyncio
async def test_complete_registration_success(client, db_session):
    email = "flow@example.com"
    # Step 1: request OTP (creates pending user).
    await client.post("/api/v1/auth/request-email-otp", json={"email": email})

    # Grab the pending user and replace its OTP with a known value we control.
    user = (await db_session.execute(select(User).where(User.email == email))).scalar_one()
    known_otp = "123456"
    await db_session.execute(
        AuthToken.__table__.delete().where(AuthToken.user_id == user.id)
    )
    db_session.add(
        AuthToken(
            user_id=user.id,
            token_hash=hashlib.sha256(known_otp.encode()).hexdigest(),
            purpose="verification",
        )
    )
    await db_session.commit()

    # Step 2: complete registration with the OTP + password + profile.
    response = await client.post(
        "/api/v1/auth/complete-registration",
        json={
            "email": email,
            "otp": known_otp,
            "password": "supersecret123",
            "first_name": "Flow",
            "last_name": "Tester",
            "phone": "+254700000111",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["user"]["verified"] is True
    assert data["user"]["first_name"] == "Flow"
    assert data["user"]["phone"] == "+254700000111"
    assert "access_token" in data["tokens"]

    # The account can now log in with the chosen password.
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret123"}
    )
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_complete_registration_bad_otp(client):
    email = "badotp@example.com"
    await client.post("/api/v1/auth/request-email-otp", json={"email": email})
    response = await client.post(
        "/api/v1/auth/complete-registration",
        json={
            "email": email,
            "otp": "000000",
            "password": "supersecret123",
            "first_name": "Bad",
            "last_name": "Otp",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_otp"
