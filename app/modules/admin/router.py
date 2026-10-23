"""Admin API (/api/v1/admin/*): Trimmy's team, signed in with both steps (require_admin).
Every change takes a reason and leaves an audit row (app/core/audit.py) in the same commit.
nginx also limits /api/v1/admin to the team's IPs."""
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.audit import AuditLog
from app.core.database import get_db
from app.core.dependencies import require_admin
from app.modules.admin.auth import admin_user, client_ip
from app.modules.admin.schemas import (
    AdminUser, AuditEntry, AuditPage, GrantAdminRequest, ReasonRequest, TeamResponse, UserStatusResponse,
)
from app.modules.auth.models import RefreshToken, User

router = APIRouter(prefix="/admin", tags=["Admin"])

AUDIT_PAGE = 50


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


async def _user(db: AsyncSession, user_id: str) -> User:
    try:
        user = await db.get(User, uuid_pkg.UUID(user_id))
    except ValueError:
        user = None
    if user is None:
        raise _error(404, "not_found", "No user with that id")
    return user


async def _end_sessions(db: AsyncSession, user_id) -> int:
    """Every refresh token goes: each app and the admin are signed out within an access token's life."""
    result = await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
    return result.rowcount or 0


@router.get("/me", response_model=AdminUser)
async def me(admin: User = Depends(require_admin)):
    return admin_user(admin)


# ─── Team ────────────────────────────────────────────────────


@router.get("/team", response_model=TeamResponse)
async def team(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    admins = (await db.execute(
        select(User).where(User.platform_role == "admin").order_by(User.created_at)
    )).scalars().all()
    return TeamResponse(admins=[admin_user(a) for a in admins])


@router.post("/team", response_model=AdminUser)
async def grant_admin(data: GrantAdminRequest, request: Request, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    """Make an existing Trimmy account an admin. They sign in to the admin app with that account's
    email and password and set up their authenticator on the first sign-in."""
    email = data.email.strip().lower()
    user = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if user is None:
        raise _error(404, "not_found", "No Trimmy account with that email. They need to sign up first.")
    if not user.password_hash:
        raise _error(409, "needs_password", "That account signs in with Google or a phone code. Ask them to set a password first.")
    if user.disabled_at is not None:
        raise _error(409, "account_disabled", "That account is disabled.")
    if user.platform_role != "admin":
        user.platform_role = "admin"
        audit.record(db, actor=admin, action="admin.grant", target_type="user", target_id=user.id,
                     reason=data.reason, ip=client_ip(request))
        await db.commit()
    return admin_user(user)


@router.delete("/team/{user_id}", response_model=AdminUser)
async def revoke_admin(user_id: str, data: ReasonRequest, request: Request, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)):
    user = await _user(db, user_id)
    if user.platform_role != "admin":
        raise _error(404, "not_found", "That person isn't an admin")
    if user.id == admin.id:
        raise _error(409, "cannot_remove_self", "Ask another admin to remove you")
    user.platform_role = None
    user.totp_secret = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    await _end_sessions(db, user.id)
    audit.record(db, actor=admin, action="admin.revoke", target_type="user", target_id=user.id,
                 reason=data.reason, ip=client_ip(request))
    await db.commit()
    return admin_user(user)


@router.post("/team/{user_id}/reset-two-step", response_model=AdminUser)
async def reset_two_step(user_id: str, data: ReasonRequest, request: Request, admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)):
    """Lost phone: they set up a new authenticator on their next sign-in. Ends their sessions."""
    user = await _user(db, user_id)
    if user.platform_role != "admin":
        raise _error(404, "not_found", "That person isn't an admin")
    user.totp_secret = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    await _end_sessions(db, user.id)
    audit.record(db, actor=admin, action="admin.reset_two_step", target_type="user", target_id=user.id,
                 reason=data.reason, ip=client_ip(request))
    await db.commit()
    return admin_user(user)


# ─── Users: disable / enable / sign out everywhere ───────────


@router.post("/users/{user_id}/disable", response_model=UserStatusResponse)
async def disable_user(user_id: str, data: ReasonRequest, request: Request, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)):
    """No sign-in and no API access (403 account_disabled); every session ends now."""
    user = await _user(db, user_id)
    if user.platform_role == "admin":
        raise _error(409, "is_admin", "Remove their admin access first")
    ended = await _end_sessions(db, user.id)
    if user.disabled_at is None:
        user.disabled_at = datetime.now(timezone.utc)
        user.disabled_reason = data.reason
        audit.record(db, actor=admin, action="user.disable", target_type="user", target_id=user.id,
                     reason=data.reason, changes={"sessionsEnded": ended}, ip=client_ip(request))
    await db.commit()
    return UserStatusResponse(id=str(user.id), disabledAt=user.disabled_at, disabledReason=user.disabled_reason,
                              sessionsEnded=ended)


@router.post("/users/{user_id}/enable", response_model=UserStatusResponse)
async def enable_user(user_id: str, data: ReasonRequest, request: Request, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    user = await _user(db, user_id)
    if user.disabled_at is not None:
        audit.record(db, actor=admin, action="user.enable", target_type="user", target_id=user.id,
                     reason=data.reason, changes={"disabledReason": [user.disabled_reason, None]}, ip=client_ip(request))
        user.disabled_at = None
        user.disabled_reason = None
        await db.commit()
    return UserStatusResponse(id=str(user.id), disabledAt=None, disabledReason=None)


@router.post("/users/{user_id}/sign-out", response_model=UserStatusResponse)
async def sign_out_everywhere(user_id: str, data: ReasonRequest, request: Request, admin: User = Depends(require_admin),
                              db: AsyncSession = Depends(get_db)):
    """E.g. a lost phone: every device signs in again."""
    user = await _user(db, user_id)
    ended = await _end_sessions(db, user.id)
    audit.record(db, actor=admin, action="user.sign_out_everywhere", target_type="user", target_id=user.id,
                 reason=data.reason, changes={"sessionsEnded": ended}, ip=client_ip(request))
    await db.commit()
    return UserStatusResponse(id=str(user.id), disabledAt=user.disabled_at, disabledReason=user.disabled_reason,
                              sessionsEnded=ended)


# ─── Audit log ───────────────────────────────────────────────


@router.get("/audit", response_model=AuditPage)
async def audit_log(
    action: Optional[str] = Query(None, description="Exact action, or a prefix ending in '.' (e.g. 'user.')"),
    actorId: Optional[str] = None,
    targetType: Optional[str] = None,
    targetId: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = Query(AUDIT_PAGE, ge=1, le=200),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Newest first."""
    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action.startswith(action) if action.endswith(".") else AuditLog.action == action)
    if actorId:
        try:
            query = query.where(AuditLog.actor_id == uuid_pkg.UUID(actorId))
        except ValueError:
            raise _error(400, "validation_error", "actorId isn't an id")
    if targetType:
        query = query.where(AuditLog.target_type == targetType)
    if targetId:
        query = query.where(AuditLog.target_id == targetId)
    if cursor:
        try:
            at_raw, id_raw = cursor.split("|", 1)
            at, cid = datetime.fromisoformat(at_raw), uuid_pkg.UUID(id_raw)
        except ValueError:
            raise _error(400, "validation_error", "Bad cursor")
        query = query.where(or_(AuditLog.at < at, and_(AuditLog.at == at, AuditLog.id < cid)))
    rows = (await db.execute(query.order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit + 1))).scalars().all()
    page = rows[:limit]
    return AuditPage(
        entries=[
            AuditEntry(id=str(r.id), actorId=str(r.actor_id) if r.actor_id else None, actorName=r.actor_name,
                       action=r.action, targetType=r.target_type, targetId=r.target_id, reason=r.reason,
                       changes=r.changes, ip=r.ip, at=r.at)
            for r in page
        ],
        nextCursor=f"{page[-1].at.isoformat()}|{page[-1].id}" if len(rows) > limit else None,
    )
