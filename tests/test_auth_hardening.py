import hashlib
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import get_password_hash
from app.modules.auth.models import AuthToken, AuthType, OtpSendLog, RefreshToken, User
from app.modules.business.models import Business, Staff


async def _create_otp(db_session, user_id, purpose):
    otp = "".join(secrets.choice(string.digits) for _ in range(6))
    db_session.add(AuthToken(
        user_id=user_id,
        token_hash=hashlib.sha256(otp.encode()).hexdigest(),
        purpose=purpose,
    ))
    await db_session.commit()
    return otp


def _wrong(otp):
    return f"{(int(otp) + 1) % 1_000_000:06d}"


async def _login(client, email="test@example.com", password="testpass123"):
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return response.json()["tokens"]


def _bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ─── OTP scoped to an account + attempt limits ────────────────


@pytest.mark.asyncio
async def test_verify_email_otp_is_scoped_to_email(client, unverified_user, test_user, db_session):
    otp = await _create_otp(db_session, unverified_user.id, "verification")
    response = await client.post("/api/v1/auth/verify-email", json={"email": test_user.email, "otp": otp})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_otp"


@pytest.mark.asyncio
async def test_verify_email_otp_not_accepted_in_query_string(client, unverified_user, db_session):
    otp = await _create_otp(db_session, unverified_user.id, "verification")
    response = await client.post(f"/api/v1/auth/verify-email?otp={otp}")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_verify_email_locks_after_max_attempts(client, unverified_user, db_session):
    otp = await _create_otp(db_session, unverified_user.id, "verification")
    body = {"email": unverified_user.email, "otp": _wrong(otp)}
    for _ in range(settings.OTP_MAX_ATTEMPTS - 1):
        response = await client.post("/api/v1/auth/verify-email", json=body)
        assert response.json()["error"] == "invalid_otp"

    response = await client.post("/api/v1/auth/verify-email", json=body)
    assert response.status_code == 400
    assert response.json()["error"] == "otp_attempts_exceeded"

    # The correct code no longer works either.
    response = await client.post("/api/v1/auth/verify-email", json={"email": unverified_user.email, "otp": otp})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_otp"


@pytest.mark.asyncio
async def test_reset_password_otp_is_scoped_to_email(client, test_user, unverified_user, db_session):
    otp = await _create_otp(db_session, test_user.id, "password_reset")
    response = await client.post("/api/v1/auth/reset-password", json={
        "email": unverified_user.email, "otp": otp, "new_password": "newpassword123",
    })
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_otp"


@pytest.mark.asyncio
async def test_reset_password_revokes_sessions(client, test_user, db_session):
    tokens = await _login(client)
    otp = await _create_otp(db_session, test_user.id, "password_reset")
    response = await client.post("/api/v1/auth/reset-password", json={
        "email": test_user.email, "otp": otp, "new_password": "newpassword123",
    })
    assert response.status_code == 200
    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401


# ─── verify-reset-otp ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_reset_otp_valid_does_not_consume(client, test_user, db_session):
    otp = await _create_otp(db_session, test_user.id, "password_reset")
    response = await client.post("/api/v1/auth/verify-reset-otp", json={"email": test_user.email, "otp": otp})
    assert response.status_code == 200
    assert response.json() == {"valid": True}

    response = await client.post("/api/v1/auth/reset-password", json={
        "email": test_user.email, "otp": otp, "new_password": "newpassword123",
    })
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_verify_reset_otp_invalid(client, test_user, db_session):
    otp = await _create_otp(db_session, test_user.id, "password_reset")
    response = await client.post("/api/v1/auth/verify-reset-otp", json={"email": test_user.email, "otp": _wrong(otp)})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_otp"


# ─── resend-otp cooldown ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resend_otp_cooldown(client, test_user):
    body = {"email": test_user.email, "purpose": "password_reset"}
    assert (await client.post("/api/v1/auth/resend-otp", json=body)).status_code == 200

    response = await client.post("/api/v1/auth/resend-otp", json=body)
    assert response.status_code == 429
    data = response.json()
    assert data["error"] == "too_many_requests"
    assert 0 < data["retry_after_seconds"] <= settings.OTP_RESEND_COOLDOWN_SECONDS
    assert response.headers["retry-after"] == str(data["retry_after_seconds"])


@pytest.mark.asyncio
async def test_resend_otp_cooldown_applies_to_unknown_email(client):
    body = {"email": "nobody@example.com", "purpose": "verification"}
    assert (await client.post("/api/v1/auth/resend-otp", json=body)).status_code == 200
    assert (await client.post("/api/v1/auth/resend-otp", json=body)).status_code == 429


@pytest.mark.asyncio
async def test_resend_otp_hourly_cap(client, test_user, db_session):
    now = datetime.now(timezone.utc)
    for i in range(settings.OTP_MAX_SENDS_PER_HOUR):
        db_session.add(OtpSendLog(
            email=test_user.email, purpose="verification",
            created_at=now - timedelta(minutes=50 - i),
        ))
    await db_session.commit()

    response = await client.post("/api/v1/auth/resend-otp", json={"email": test_user.email, "purpose": "verification"})
    assert response.status_code == 429
    # Oldest counted send was 50 minutes ago, so roughly 10 minutes to wait.
    assert 9 * 60 <= response.json()["retry_after_seconds"] <= 10 * 60


# ─── logout ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token(client, test_user):
    tokens = await _login(client)
    response = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=_bearer(tokens),
    )
    assert response.status_code == 204
    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_requires_auth(client, test_user):
    tokens = await _login(client)
    response = await client.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_ignores_other_users_token(client, test_user, db_session):
    other = User(
        id=uuid.uuid4(), email="other@example.com", password_hash=get_password_hash("otherpass123"),
        auth_type=AuthType.EMAIL, verified=True,
    )
    db_session.add(other)
    await db_session.commit()
    mine = await _login(client)
    theirs = await _login(client, "other@example.com", "otherpass123")

    response = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": theirs["refresh_token"]}, headers=_bearer(mine),
    )
    assert response.status_code == 204
    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": theirs["refresh_token"]})
    assert response.status_code == 200


# ─── PATCH /auth/me ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_me(client, test_user, auth_headers):
    response = await client.patch("/api/v1/auth/me", json={"first_name": "Jane"}, headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["first_name"] == "Jane"
    assert data["last_name"] == "User"
    assert data["phone"] == "+1234567890"


@pytest.mark.asyncio
async def test_update_me_clear_phone(client, test_user, auth_headers):
    response = await client.patch("/api/v1/auth/me", json={"phone": None}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["phone"] is None


@pytest.mark.asyncio
async def test_update_me_phone_taken(client, test_user, auth_headers, db_session):
    db_session.add(User(
        id=uuid.uuid4(), email="p@example.com", phone="+254700000000",
        auth_type=AuthType.EMAIL, verified=True,
    ))
    await db_session.commit()
    response = await client.patch("/api/v1/auth/me", json={"phone": "+254700000000"}, headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["error"] == "phone_taken"


@pytest.mark.asyncio
async def test_update_me_null_name_rejected(client, test_user, auth_headers):
    response = await client.patch("/api/v1/auth/me", json={"first_name": None}, headers=auth_headers)
    assert response.status_code == 400


# ─── change-password ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_password(client, test_user):
    old = await _login(client)
    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "testpass123", "new_password": "brandnew123"},
        headers=_bearer(old),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Password updated"

    # Old session revoked, new tokens work, new password works.
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": old["refresh_token"]})).status_code == 401
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": data["tokens"]["refresh_token"]})).status_code == 200
    await _login(client, password="brandnew123")


@pytest.mark.asyncio
async def test_change_password_wrong_current(client, test_user, auth_headers):
    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "nope", "new_password": "brandnew123"},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_credentials"


# ─── DELETE /auth/me ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_account(client, test_user, auth_headers, db_session):
    db_session.add(Business(owner_id=test_user.id, name="Fade Studio", phone="+254711111111"))
    await db_session.commit()

    response = await client.request(
        "DELETE", "/api/v1/auth/me", json={"password": "testpass123"}, headers=auth_headers,
    )
    assert response.status_code == 204

    db_session.expire_all()
    assert (await db_session.execute(select(User).where(User.id == test_user.id))).first() is None
    assert (await db_session.execute(select(Business))).first() is None
    assert (await db_session.execute(select(RefreshToken))).first() is None
    assert (await client.get("/api/v1/auth/me", headers=auth_headers)).status_code == 401


@pytest.mark.asyncio
async def test_delete_account_wrong_password(client, test_user, auth_headers):
    response = await client.request(
        "DELETE", "/api/v1/auth/me", json={"password": "wrong"}, headers=auth_headers,
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_credentials"


# ─── check-email ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_email(client, test_user):
    response = await client.post("/api/v1/auth/check-email", json={"email": test_user.email})
    assert response.json() == {"exists": True}
    response = await client.post("/api/v1/auth/check-email", json={"email": "nobody@example.com"})
    assert response.json() == {"exists": False}


@pytest.mark.asyncio
async def test_check_email_rate_limited(client):
    limit = int(settings.RATE_LIMIT_CHECK_EMAIL.split("/")[0])
    for _ in range(limit):
        assert (await client.post("/api/v1/auth/check-email", json={"email": "a@example.com"})).status_code == 200
    response = await client.post("/api/v1/auth/check-email", json={"email": "a@example.com"})
    assert response.status_code == 429
    assert response.json()["error"] == "too_many_requests"


# ─── GET /me/businesses ───────────────────────────────────────


@pytest.mark.asyncio
async def test_my_businesses(client, test_user, auth_headers, db_session):
    other_owner = User(id=uuid.uuid4(), email="boss@example.com", auth_type=AuthType.EMAIL, verified=True)
    db_session.add(other_owner)
    mine = Business(owner_id=test_user.id, name="Fade Studio", phone="+254711111111", status="published")
    work = Business(owner_id=other_owner.id, name="Cuts Co", phone="+254722222222", status="published")
    db_session.add_all([mine, work])
    await db_session.flush()
    db_session.add_all([
        Staff(business_id=mine.id, user_id=test_user.id, name="Test User", role="owner"),
        Staff(business_id=work.id, user_id=test_user.id, name="Test User", role="front_desk"),
    ])
    await db_session.commit()

    response = await client.get("/api/v1/me/businesses", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"businesses": [
        {"businessId": str(mine.id), "name": "Fade Studio", "status": "published", "role": "owner"},
        {"businessId": str(work.id), "name": "Cuts Co", "status": "published", "role": "front_desk"},
    ]}


@pytest.mark.asyncio
async def test_my_businesses_empty(client, test_user, auth_headers):
    response = await client.get("/api/v1/me/businesses", headers=auth_headers)
    assert response.json() == {"businesses": []}
