"""Staff payout requests (fulfillment.json#request-payout, S3/O4).

Trimmy never moves this money: the owner pays the staff member themselves
(M-Pesa, cash) and "approve" records that they did.
"""
import uuid as uuid_pkg
from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.db_types import UUID, uuid4


class PayoutStatus(str, PyEnum):
    PENDING = "pending"
    PAID = "paid"
    REJECTED = "rejected"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # Kept (set null) if the staff member is later removed, so the history survives.
    staff_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("staff.id", ondelete="SET NULL"), nullable=True
    )
    staff_name: Mapped[str] = mapped_column(Text, nullable=False)  # snapshot for the history
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KES")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=PayoutStatus.PENDING)
    requested_by: Mapped[uuid_pkg.UUID] = mapped_column(UUID, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    responded_by: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, nullable=True)
    responded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_payouts_business_status", "business_id", "status"),
        Index("ix_payouts_staff", "staff_id"),
    )
