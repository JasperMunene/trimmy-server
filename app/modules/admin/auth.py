"""Admin sign-in: email + password, then a code from an authenticator app (required; set up on
the first sign-in). Admin sessions are short (ADMIN_ACCESS_TOKEN_MINUTES / ADMIN_SESSION_HOURS)
and carry scope=admin + mfa, which is what require_admin checks. A wrong password and a
non-admin account get the same answer, so the endpoint doesn't reveal who the admins are.
"""
import hashlib
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, totp
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import require_admin
from app.core.rate_limit import clear_login_failures, limiter, login_locked, record_login_failure
from app.core.security import create_access_token, create_refresh_token, decode_token, verify_password
from app.modules.admin.schemas import (
    AdminLoginRequest, AdminLoginResponse, AdminRefreshRequest, AdminSessionResponse, AdminTokens,
    AdminUser, AdminVerifyRequest,
)
from app.modules.auth.models import RefreshToken, User

router = APIRouter(prefix="/admin/auth", tags=["Admin"])

CHALLENGE_MINUTES = 5


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _invalid() -> HTTPException:
    return _error(401, "invalid_credentials", "Invalid email or password")


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def admin_user(user: User) -> AdminUser:
    return AdminUser(id=str(user.id), email=user.email, firstName=user.first_name, lastName=user.last_name,
                     avatarUrl=user.avatar_url, twoStepSince=user.totp_enabled_at)


def _challenge(user: User, kind: str, **extra) -> str:
    payload = {"sub": str(user.id), "type": kind,
               "exp": datetime.now(timezone.utc) + timedelta(minutes=CHALLENGE_MINUTES), **extra}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_admin(user: User | None) -> bool:
    return user is not None and user.platform_role == "admin" and user.disabled_at is None


def _issue(db: AsyncSession, user: User) -> AdminTokens:
    access = create_access_token(
        {"sub": str(user.id), "scope": "admin", "mfa": True},
        expires_delta=timedelta(minutes=settings.ADMIN_ACCESS_TOKEN_MINUTES),
    )
    session = timedelta(hours=settings.ADMIN_SESSION_HOURS)
    refresh = create_refresh_token({"sub": str(user.id), "scope": "admin"}, expires_delta=session)
    db.add(RefreshToken(user_id=user.id, token_hash=_hash(refresh), expires_at=datetime.now(timezone.utc) + session))
    return AdminTokens(access_token=access, refresh_token=refresh, expires_in=settings.ADMIN_ACCESS_TOKEN_MINUTES * 60)


@router.post("/login", response_model=AdminLoginResponse)
@limiter.limit(settings.RATE_LIMIT_LOGIN)
async def admin_login(request: Request, data: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    email = data.email.strip().lower()
    if login_locked(email):
        raise _error(429, "too_many_attempts", "Too many attempts. Try again in a few minutes.")
    user = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if not user or not user.password_hash or not verify_password(data.password, user.password_hash) or not _is_admin(user):
        record_login_failure(email)
        raise _invalid()
    if user.totp_enabled_at and user.totp_secret:
        return AdminLoginResponse(step="totp", challenge=_challenge(user, "admin_challenge"))
    # First sign-in: set up the authenticator. The secret rides (encrypted) in the challenge
    # and is only saved once a code from it proves the app has it.
    secret = totp.new_secret()
    return AdminLoginResponse(
        step="enroll",
        challenge=_challenge(user, "admin_enroll", sec=totp.encrypt(secret)),
        secret=secret,
        otpauthUrl=totp.otpauth_url(secret, user.email or str(user.id)),
    )


@router.post("/verify", response_model=AdminSessionResponse)
@limiter.limit(settings.RATE_LIMIT_LOGIN)
async def admin_verify(request: Request, data: AdminVerifyRequest, db: AsyncSession = Depends(get_db)):
    payload = decode_token(data.challenge)
    if not payload or payload.get("type") not in ("admin_challenge", "admin_enroll"):
        raise _error(401, "challenge_expired", "That sign-in took too long. Start again.")
    user = await db.get(User, _uuid(payload.get("sub")))
    if not _is_admin(user):
        raise _invalid()
    email = (user.email or "").lower()
    if login_locked(email):
        raise _error(429, "too_many_attempts", "Too many attempts. Try again in a few minutes.")

    enrolling = payload["type"] == "admin_enroll"
    secret = totp.decrypt(payload.get("sec", "") if enrolling else (user.totp_secret or ""))
    step = totp.verify(secret, data.code, None if enrolling else user.totp_last_step) if secret else None
    if step is None:
        record_login_failure(email)
        raise _error(401, "invalid_code", "That code didn't work. Check the time on your phone and try the newest code.")

    if enrolling:
        user.totp_secret = totp.encrypt(secret)
        user.totp_enabled_at = datetime.now(timezone.utc)
        audit.record(db, actor=user, action="admin.two_step_setup", target_type="user", target_id=user.id,
                     ip=client_ip(request))
    user.totp_last_step = step
    clear_login_failures(email)
    tokens = _issue(db, user)
    audit.record(db, actor=user, action="admin.sign_in", target_type="user", target_id=user.id, ip=client_ip(request))
    await db.commit()
    return AdminSessionResponse(user=admin_user(user), tokens=tokens)


@router.post("/refresh", response_model=AdminSessionResponse)
async def admin_refresh(data: AdminRefreshRequest, db: AsyncSession = Depends(get_db)):
    """Rotates the admin session. A reused (already rotated) token ends every session of that user."""
    payload = decode_token(data.refresh_token)
    if not payload or payload.get("type") != "refresh" or payload.get("scope") != "admin":
        raise _error(401, "invalid_refresh_token", "Sign in again")
    stored = (await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash(data.refresh_token))
    )).scalar_one_or_none()
    if not stored or str(stored.user_id) != payload.get("sub"):
        raise _error(401, "invalid_refresh_token", "Sign in again")
    if stored.revoked:
        from app.modules.auth.service import AuthService

        await AuthService(db)._revoke_all_user_tokens(stored.user_id)
        await db.commit()
        raise _error(401, "refresh_token_reused", "Sign in again")
    expires = stored.expires_at if stored.expires_at.tzinfo else stored.expires_at.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise _error(401, "refresh_token_expired", "Sign in again")
    user = await db.get(User, stored.user_id)
    if not _is_admin(user):
        raise _error(403, "not_admin", "Admin access required")
    stored.revoked = True
    tokens = _issue(db, user)
    await db.commit()
    return AdminSessionResponse(user=admin_user(user), tokens=tokens)


@router.post("/logout", status_code=204)
async def admin_logout(data: AdminRefreshRequest, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    stored = (await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash(data.refresh_token))
    )).scalar_one_or_none()
    if stored and stored.user_id == admin.id:
        await db.delete(stored)
        await db.commit()


def _uuid(value):
    import uuid as uuid_pkg

    try:
        return uuid_pkg.UUID(str(value))
    except (ValueError, TypeError):
        return None
