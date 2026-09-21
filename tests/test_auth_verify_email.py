import hashlib
import pytest
from sqlalchemy import select

from app.modules.auth.models import AuthToken


async def _create_otp(db_session, user_id, purpose="verification"):
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
async def test_verify_email_success(client, unverified_user, db_session):
    otp = await _create_otp(db_session, unverified_user.id)
    response = await client.post(f"/api/v1/auth/verify-email?otp={otp}")
    assert response.status_code == 200
    data = response.json()
    assert data["user"]["verified"] is True
    assert "access_token" in data["tokens"]


@pytest.mark.asyncio
async def test_verify_email_invalid_otp(client, unverified_user):
    response = await client.post("/api/v1/auth/verify-email?otp=000000")
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "invalid_otp"


@pytest.mark.asyncio
async def test_verify_email_expired_otp(client, unverified_user, db_session):
    from datetime import datetime, timedelta, timezone
    otp = await _create_otp(db_session, unverified_user.id)
    result = await db_session.execute(
        select(AuthToken).where(AuthToken.user_id == unverified_user.id)
    )
    token = result.scalar_one()
    token.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    await db_session.commit()

    response = await client.post(f"/api/v1/auth/verify-email?otp={otp}")
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "otp_expired"
