"""Reports: someone telling Trimmy's team about a business, a review, a customer or a booking.
Reports on the same thing group into one case in the admin (safety/admin.py)."""
import uuid as uuid_pkg
from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.db_types import UUID, uuid4


class ReportTarget(str, PyEnum):
    BUSINESS = "business"
    REVIEW = "review"
    CUSTOMER = "customer"  # a business's customer record (customers.BusinessCustomer)
    BOOKING = "booking"


class ReportReason(str, PyEnum):
    FRAUD = "fraud"                  # scam, fake business, took money
    HARASSMENT = "harassment"
    INAPPROPRIATE = "inappropriate"  # offensive photos or words
    FAKE_REVIEW = "fake_review"
    SAFETY = "safety"                # felt unsafe
    NO_SHOW = "no_show"              # a customer who keeps not turning up
    OTHER = "other"


class ReportStatus(str, PyEnum):
    OPEN = "open"
    ACTIONED = "actioned"   # something was done
    DISMISSED = "dismissed"  # looked at, nothing to do


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    reporter_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # The business it's about (the reported one, or where the review / customer / booking is).
    business_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, ForeignKey("businesses.id", ondelete="SET NULL"), nullable=True)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ReportStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_reports_target", "target_type", "target_id", "status"),
        Index("ix_reports_status_created", "status", "created_at"),
    )
