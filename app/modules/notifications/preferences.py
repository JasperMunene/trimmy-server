"""GET/PATCH /me/notification-preferences: what someone hears about, and how.

Every notification is always in the in-app inbox; these settings choose the extra channels (push,
SMS, email) per category. Reminders also pick which ones to send (24h / 2h) — the original C5
settings, still in their own columns and still under `reminders` for older app versions.
"""
import uuid as uuid_pkg
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.auth.models import User
from app.modules.notifications.models import NotificationPreferences

router = APIRouter(prefix="/me/notification-preferences", tags=["Notifications"])

CHANNELS = ("push", "sms", "email")

# What each category is on by default. In-app is always on and not listed.
DEFAULTS: dict[str, dict[str, bool]] = {
    "bookings": {"push": True, "sms": True, "email": True},  # your bookings: confirmed, changed, cancelled, receipts
    "reminders": {"push": True, "sms": True, "email": False},  # 24h / 2h before
    "business": {"push": True, "sms": False, "email": False},  # bookings at a business you run or work at
    "payouts": {"push": True, "sms": False, "email": False},  # payout requests and answers, wallet changes
    "team": {"push": True, "sms": True, "email": True},  # invitations
}

# Which category a notification type belongs to. Types not listed (e.g. "test") always go out on
# every channel that's switched on.
TYPE_CATEGORY: dict[str, str] = {
    **{t: "bookings" for t in (
        "booking_confirmed", "booking_cancelled_by_business", "booking_rescheduled_by_business",
        "booking_payment_failed", "booking_hold_expired", "booking_refund_due", "booking_refunded",
        "booking_no_show", "booking_completed",
    )},
    "booking_reminder_24h": "reminders",
    "booking_reminder_2h": "reminders",
    **{t: "business" for t in (
        "booking_created", "booking_cancelled_by_customer", "booking_rescheduled_by_customer",
        "booking_assigned", "booking_cancelled_for_staff", "booking_refund_owed", "day_summary_submitted",
    )},
    **{t: "payouts" for t in ("payout_requested", "payout_responded", "wallet_adjusted")},
    "team_invitation": "team",
}


@dataclass(frozen=True)
class ReminderPrefs:
    day_before: bool = True
    two_hours_before: bool = True
    sms: bool = True
    push: bool = True
    email: bool = False

    def wants(self, kind: str) -> bool:
        return self.day_before if kind == "24h" else self.two_hours_before


@dataclass(frozen=True)
class Prefs:
    reminders: ReminderPrefs
    channels: dict[str, dict[str, bool]]  # every category, defaults filled in

    def allows(self, notification_type: str, channel: str) -> bool:
        category = TYPE_CATEGORY.get(notification_type)
        return True if category is None else self.channels[category][channel]


def _prefs(row: Optional[NotificationPreferences]) -> Prefs:
    overrides = (row.channels or {}) if row else {}
    channels = {c: {**d, **{k: v for k, v in (overrides.get(c) or {}).items() if k in CHANNELS}} for c, d in DEFAULTS.items()}
    if row is not None:  # reminders' push/sms live in their own columns
        channels["reminders"].update(sms=row.reminder_sms, push=row.reminder_push)
    r = channels["reminders"]
    reminders = ReminderPrefs(row.reminder_24h if row else True, row.reminder_2h if row else True,
                              r["sms"], r["push"], r["email"])
    return Prefs(reminders, channels)


async def prefs_for(db: AsyncSession, user_ids: list[uuid_pkg.UUID]) -> dict[uuid_pkg.UUID, Prefs]:
    """Everyone's settings in one query; people who never changed anything get the defaults."""
    rows = {}
    if user_ids:
        rows = {r.user_id: r for r in (await db.execute(
            select(NotificationPreferences).where(NotificationPreferences.user_id.in_(user_ids))
        )).scalars()}
    return {uid: _prefs(rows.get(uid)) for uid in user_ids}


async def reminder_prefs(db: AsyncSession, user_id: Optional[uuid_pkg.UUID]) -> ReminderPrefs:
    """Everything on for people who never changed anything (and for bookings without an account)."""
    row = await db.get(NotificationPreferences, user_id) if user_id else None
    return _prefs(row).reminders


# ─── API ─────────────────────────────────────────────────────


class Reminders(BaseModel):
    day_before: bool = Field(alias="dayBefore")
    two_hours_before: bool = Field(alias="twoHoursBefore")
    sms: bool
    push: bool
    email: bool = False

    model_config = {"populate_by_name": True}


class RemindersPatch(BaseModel):
    day_before: Optional[bool] = Field(None, alias="dayBefore")
    two_hours_before: Optional[bool] = Field(None, alias="twoHoursBefore")
    sms: Optional[bool] = None
    push: Optional[bool] = None
    email: Optional[bool] = None

    model_config = {"populate_by_name": True}


class ChannelSwitches(BaseModel):
    push: bool
    sms: bool
    email: bool


class ChannelPatch(BaseModel):
    push: Optional[bool] = None
    sms: Optional[bool] = None
    email: Optional[bool] = None


class NotificationPreferencesResponse(BaseModel):
    reminders: Reminders
    # Only the categories that apply to this person (e.g. `business` once they run or work at one).
    categories: dict[str, ChannelSwitches]
    # Which channels can reach them at all: switched on for Trimmy, and they have a phone / email.
    availableChannels: ChannelSwitches


class UpdateNotificationPreferencesRequest(BaseModel):
    reminders: Optional[RemindersPatch] = None
    categories: Optional[dict[str, ChannelPatch]] = None


async def _categories_for(db: AsyncSession, user: User) -> list[str]:
    from app.modules.business.models import Business, Staff

    owns = (await db.execute(select(exists().where(Business.owner_id == user.id)))).scalar()
    roles = set((await db.execute(select(Staff.role).where(Staff.user_id == user.id))).scalars().all())
    available = ["bookings", "reminders"]
    if owns or roles:
        available.append("business")
    if owns or (roles - {"front_desk"}):
        available.append("payouts")
    available.append("team")
    return available


async def _response(db: AsyncSession, user: User) -> NotificationPreferencesResponse:
    p = (await prefs_for(db, [user.id]))[user.id]
    r = p.reminders
    return NotificationPreferencesResponse(
        reminders=Reminders(dayBefore=r.day_before, twoHoursBefore=r.two_hours_before, sms=r.sms, push=r.push, email=r.email),
        categories={c: ChannelSwitches(**p.channels[c]) for c in await _categories_for(db, user)},
        availableChannels=ChannelSwitches(
            push=settings.NOTIFY_PUSH_ENABLED,
            sms=settings.NOTIFY_SMS_ENABLED and bool(user.phone),
            email=settings.NOTIFY_EMAIL_ENABLED and bool(user.email),
        ),
    )


@router.get("", response_model=NotificationPreferencesResponse)
async def get_notification_preferences(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _response(db, current_user)


@router.patch("", response_model=NotificationPreferencesResponse)
async def update_notification_preferences(
    data: UpdateNotificationPreferencesRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Only the fields sent change. Unknown categories are ignored."""
    row = await db.get(NotificationPreferences, current_user.id)
    if row is None:
        row = NotificationPreferences(user_id=current_user.id, reminder_24h=True, reminder_2h=True,
                                      reminder_sms=True, reminder_push=True)
        db.add(row)
    channels = {k: dict(v) for k, v in (row.channels or {}).items()}

    if data.reminders is not None:
        r = data.reminders
        for field, column in (("day_before", "reminder_24h"), ("two_hours_before", "reminder_2h"),
                              ("sms", "reminder_sms"), ("push", "reminder_push")):
            value = getattr(r, field)
            if value is not None:
                setattr(row, column, value)
        if r.email is not None:
            channels.setdefault("reminders", {})["email"] = r.email
    for category, patch in (data.categories or {}).items():
        if category not in DEFAULTS:
            continue
        for channel in CHANNELS:
            value = getattr(patch, channel)
            if value is None:
                continue
            if category == "reminders" and channel in ("sms", "push"):
                setattr(row, f"reminder_{channel}", value)
            else:
                channels.setdefault(category, {})[channel] = value
    row.channels = channels or None  # a new dict, so the JSON change is saved
    await db.commit()
    return await _response(db, current_user)
