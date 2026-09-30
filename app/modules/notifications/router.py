from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.auth.models import User
from app.modules.notifications.models import PushToken
from app.modules.notifications.push import is_expo_token

router = APIRouter(prefix="/me/push-tokens", tags=["Notifications"])


class OkResponse(BaseModel):
    ok: bool = True


class RegisterPushTokenRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=200, examples=["ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]"])
    platform: str = Field(..., pattern=r"^(ios|android)$")


@router.post("", status_code=status.HTTP_200_OK, response_model=OkResponse)
async def register_push_token(
    data: RegisterPushTokenRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register this device for notifications. Idempotent; a token moves to whoever registers it last
    (e.g. someone else signs in on the same phone)."""
    if not is_expo_token(data.token):
        raise HTTPException(status_code=400, detail={"error": "invalid_token", "message": "Not an Expo push token"})
    existing = (await db.execute(select(PushToken).where(PushToken.token == data.token))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if existing is None:
        db.add(PushToken(user_id=current_user.id, token=data.token, platform=data.platform))
    else:
        existing.user_id, existing.platform, existing.last_seen_at = current_user.id, data.platform, now
    await db.commit()
    return OkResponse()


@router.delete("/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_push_token(
    token: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """On logout: stop notifying this device. Idempotent."""
    await db.execute(delete(PushToken).where(PushToken.token == token, PushToken.user_id == current_user.id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
