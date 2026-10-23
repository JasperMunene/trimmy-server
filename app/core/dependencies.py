from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from app.core.database import get_db
from app.core.security import decode_token
from app.modules.auth.models import User

security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_token", "message": "Authentication required"},
        )

    payload = decode_token(credentials.credentials)
    if payload is None or payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "message": "Invalid or expired token"},
        )

    user_id: str = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "message": "Invalid token payload"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "user_not_found", "message": "User no longer exists"},
        )
    if user.disabled_at is not None:
        raise account_disabled()

    return user


def account_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "account_disabled",
            "message": "This account has been disabled. Contact Trimmy support if you think this is a mistake.",
        },
    )


async def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Trimmy's team, signed in to the admin app with both steps. Normal app tokens never pass:
    only tokens from POST /admin/auth/verify carry scope=admin and mfa."""
    payload = decode_token(credentials.credentials) if credentials else None
    if not payload or payload.get("type") != "access" or payload.get("scope") != "admin" or payload.get("mfa") is not True:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "admin_auth_required", "message": "Sign in to the admin app"},
        )
    user = await db.get(User, _uuid(payload.get("sub")))
    if user is None or user.disabled_at is not None or user.platform_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "not_admin", "message": "Admin access required"},
        )
    return user


def _uuid(value):
    import uuid as uuid_pkg

    try:
        return uuid_pkg.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def get_current_verified_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "not_verified", "message": "Email not verified"},
        )
    return current_user
