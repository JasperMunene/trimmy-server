import hashlib

import pytest
from sqlalchemy import select

from app.modules.auth.models import AuthToken, User


@pytest.mark.asyncio
async def test_request_phone_otp_creates_pending_user(client, db_session):
    phone = "+254712345678"
    response = await client.post("/api/v1/auth/request-phone-otp", json={"phone": "0712345678"})
    assert response.status_code == 200

    # Number is normalised to E.164 and a pending, password-less user exists.
    user = (await db_session.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    assert user is not None
    assert user.password_hash is None
    assert user.verified is False
    assert user.auth_type == "phone"

    token = (
        await db_session.execute(
            select(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == "verification")
        )
    ).scalar_one_or_none()
    assert token is not None


@pytest.mark.asyncio
async def test_request_phone_otp_invalid_number(client):
    # Long enough to pass the schema length check, but not a valid phone number.
    response = await client.post("/api/v1/auth/request-phone-otp", json={"phone": "0000000"})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_phone"


@pytest.mark.asyncio
async def test_complete_phone_registration_success(client, db_session):
    await client.post("/api/v1/auth/request-phone-otp", json={"phone": "0712345678"})

    user = (
        await db_session.execute(select(User).where(User.phone == "+254712345678"))
    ).scalar_one()
    known_otp = "123456"
    await db_session.execute(AuthToken.__table__.delete().where(AuthToken.user_id == user.id))
    db_session.add(
        AuthToken(
            user_id=user.id,
            token_hash=hashlib.sha256(known_otp.encode()).hexdigest(),
            purpose="verification",
        )
    )
    await db_session.commit()

    response = await client.post(
        "/api/v1/auth/complete-phone-registration",
        json={
            "phone": "0712345678",
            "otp": known_otp,
            "password": "supersecret123",
            "first_name": "Phone",
            "last_name": "User",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["user"]["verified"] is True
    assert data["user"]["phone"] == "+254712345678"
    assert data["user"]["first_name"] == "Phone"
    assert "access_token" in data["tokens"]


@pytest.mark.asyncio
async def test_complete_phone_registration_bad_otp(client):
    await client.post("/api/v1/auth/request-phone-otp", json={"phone": "0712345678"})
    response = await client.post(
        "/api/v1/auth/complete-phone-registration",
        json={
            "phone": "0712345678",
            "otp": "000000",
            "password": "supersecret123",
            "first_name": "Bad",
            "last_name": "Otp",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_otp"
