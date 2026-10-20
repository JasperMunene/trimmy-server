import uuid as uuid_pkg
from datetime import datetime, timezone

from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.db_types import UUID, uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PushToken(Base):
    """An Expo push token for one device (BK-41). A token belongs to whoever registered it last."""

    __tablename__ = "push_tokens"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    user_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(String(10), nullable=False)  # ios | android
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class NotificationPreferences(Base):
    """What someone wants to hear about, and how (preferences.py). No row = the defaults.
    Reminders keep their own columns (the original C5 settings); everything else is in `channels`."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    reminder_24h: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reminder_2h: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reminder_sms: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reminder_push: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Overrides only, e.g. {"bookings": {"email": false}, "reminders": {"email": true}}.
    channels: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class Notification(Base):
    """One entry in a user's in-app inbox. Every notification is one of these first; push, SMS
    and email are extra channels (see dispatch.py). `type` and `data` are what the app routes on,
    the same as a push's data."""

    __tablename__ = "notifications"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    user_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # One event, one row per person: a retried dispatch finds its rows instead of adding more.
    event_key: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "event_key", name="uq_notifications_user_event"),
        # The inbox page and the stream's catch-up both read a user's newest rows.
        Index("ix_notifications_user_created", "user_id", "created_at"),
        # The bell's unread count: only unread rows are indexed.
        Index("ix_notifications_user_unread", "user_id",
              postgresql_where=text("read_at IS NULL"), sqlite_where=text("read_at IS NULL")),
    )


class NotificationDelivery(Base):
    """What happened when a notification went out on an outside channel (push for now).
    A retry only resends to channels without a `sent` row."""

    __tablename__ = "notification_deliveries"
    # One row per notification and channel: claimed before sending, so a retry never sends twice.
    __table_args__ = (UniqueConstraint("notification_id", "channel", name="uq_notification_deliveries_channel"),)

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    notification_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(10), nullable=False)  # push | sms | email
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # sending | sent | failed
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
