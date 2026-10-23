"""Audit log: what Trimmy's team did, to what, why. Written by every admin action
(the BookingEvent idea, for the whole platform). Rows are never edited or deleted."""
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.db_types import UUID, uuid4


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    # Who did it (null: the system). Kept if the account goes later.
    actor_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # e.g. user.disable, admin.grant, admin.sign_in
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    target_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # What changed: {"field": [before, after]} or any detail worth keeping.
    changes: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        Index("ix_audit_log_at", "at"),
        Index("ix_audit_log_target", "target_type", "target_id"),
    )


def record(
    db: AsyncSession,
    *,
    actor,  # User or None
    action: str,
    target_type: Optional[str] = None,
    target_id: Any = None,
    reason: Optional[str] = None,
    changes: Optional[dict] = None,
    ip: Optional[str] = None,
) -> AuditLog:
    """Add an audit row to the session; it commits with the change it describes."""
    entry = AuditLog(
        actor_id=getattr(actor, "id", None),
        actor_name=_name(actor),
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        reason=reason,
        changes=changes,
        ip=ip,
    )
    db.add(entry)
    return entry


def _name(actor) -> str:
    if actor is None:
        return "System"
    full = " ".join(p for p in (getattr(actor, "first_name", None), getattr(actor, "last_name", None)) if p)
    return full or getattr(actor, "email", None) or "Admin"
