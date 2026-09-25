import hashlib
import pytest
from sqlalchemy import select

from app.modules.auth.models import AuthToken


async def _create_otp(db_session, user_id, purpose="password_reset"):
    import secrets, string
    otp = "".join(secrets.choice(string.digits) for _ in range(6))
    token = AuthToken(
        user_id=user_id,
        token_hash=hashlib.sha256(otp.encode()).hexdigest(),
        purpose=purpose,
    )
    db_session.add(token)
    await db_session.commit()
    return otp


@pytest.mark.asyncio
async def test_reset_password_success(client, test_user, db_session):
    otp = await _create_otp(db_session, test_user.id)
    response = await client.post("/api/v1/auth/reset-password", json={
        "email": test_user.email,
        "otp": otp,
        "new_password": "newpassword123",
    })
    assert response.status_code == 200
    data = response.json()
    assert "message" in data


@pytest.mark.asyncio
async def test_reset_password_invalid_otp(client, test_user):
    response = await client.post("/api/v1/auth/reset-password", json={
        "email": test_user.email,
        "otp": "000000",
        "new_password": "newpassword123",
    })
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "invalid_otp"


@pytest.mark.asyncio
async def test_reset_password_expired_otp(client, test_user, db_session):
    from datetime import datetime, timedelta, timezone
    otp = await _create_otp(db_session, test_user.id)
    result = await db_session.execute(
        select(AuthToken).where(AuthToken.user_id == test_user.id)
    )
    token = result.scalar_one()
    token.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.commit()

    response = await client.post("/api/v1/auth/reset-password", json={
        "email": test_user.email,
        "otp": otp,
        "new_password": "newpassword123",
    })
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "otp_expired"


@pytest.mark.asyncio
async def test_reset_password_short_password(client, test_user, db_session):
    otp = await _create_otp(db_session, test_user.id)
    response = await client.post("/api/v1/auth/reset-password", json={
        "email": test_user.email,
        "otp": otp,
        "new_password": "short",
    })
    assert response.status_code == 422
