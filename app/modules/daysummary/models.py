import uuid as uuid_pkg
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.db_types import UUID, uuid4


class DaySubmission(Base):
    """The front desk's end-of-day sign-off for one business day (F4)."""

    __tablename__ = "day_submissions"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    submitted_by: Mapped[uuid_pkg.UUID] = mapped_column(UUID, nullable=False)
    submitted_by_name: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("business_id", "day", name="uq_day_submissions_business_day"),)
