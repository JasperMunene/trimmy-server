"""GET/PATCH /me/notification-preferences: which booking reminders a customer gets, and how."""
import uuid as uuid_pkg
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.auth.models import User
from app.modules.notifications.models import NotificationPreferences

router = APIRouter(prefix="/me/notification-preferences", tags=["Notifications"])


@dataclass(frozen=True)
class ReminderPrefs:
    day_before: bool = True
    two_hours_before: bool = True
    sms: bool = True
    push: bool = True

    def wants(self, kind: str) -> bool:
        return self.day_before if kind == "24h" else self.two_hours_before


async def reminder_prefs(db: AsyncSession, user_id: Optional[uuid_pkg.UUID]) -> ReminderPrefs:
    """Everything on for people who never changed anything (and for bookings without an account)."""
    row = await db.get(NotificationPreferences, user_id) if user_id else None
    if row is None:
        return ReminderPrefs()
    return ReminderPrefs(row.reminder_24h, row.reminder_2h, row.reminder_sms, row.reminder_push)


class Reminders(BaseModel):
    day_before: bool = Field(alias="dayBefore")
    two_hours_before: bool = Field(alias="twoHoursBefore")
    sms: bool
    push: bool

    model_config = {"populate_by_name": True}


class RemindersPatch(BaseModel):
    day_before: Optional[bool] = Field(None, alias="dayBefore")
    two_hours_before: Optional[bool] = Field(None, alias="twoHoursBefore")
    sms: Optional[bool] = None
    push: Optional[bool] = None

    model_config = {"populate_by_name": True}


class NotificationPreferencesResponse(BaseModel):
    reminders: Reminders


class UpdateNotificationPreferencesRequest(BaseModel):
    reminders: RemindersPatch


def _response(p: ReminderPrefs) -> NotificationPreferencesResponse:
    return NotificationPreferencesResponse(
        reminders=Reminders(dayBefore=p.day_before, twoHoursBefore=p.two_hours_before, sms=p.sms, push=p.push)
    )


@router.get("", response_model=NotificationPreferencesResponse)
async def get_notification_preferences(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return _response(await reminder_prefs(db, current_user.id))


@router.patch("", response_model=NotificationPreferencesResponse)
async def update_notification_preferences(
    data: UpdateNotificationPreferencesRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Only the fields sent change."""
    row = await db.get(NotificationPreferences, current_user.id)
    if row is None:
        row = NotificationPreferences(user_id=current_user.id, reminder_24h=True, reminder_2h=True,
                                      reminder_sms=True, reminder_push=True)
        db.add(row)
    changes = data.reminders
    for field, column in (("day_before", "reminder_24h"), ("two_hours_before", "reminder_2h"),
                          ("sms", "reminder_sms"), ("push", "reminder_push")):
        value = getattr(changes, field)
        if value is not None:
            setattr(row, column, value)
    await db.commit()
    return _response(await reminder_prefs(db, current_user.id))
