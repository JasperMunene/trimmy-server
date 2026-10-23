"""Payment attempts (docs/booking/PLAN.md, section 4.1). One row per STK push."""
import uuid as uuid_pkg
from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.db_types import UUID, uuid4


class PaymentKind(str, PyEnum):
    DEPOSIT = "deposit"
    CHARGE = "charge"  # the final charge at checkout (later phase)


class PaymentStatus(str, PyEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    # For a visit checkout, the first booking charged (the rest are in payment_allocations).
    booking_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    # Set when one payment settles several bookings of a visit (POST /visits/{id}/charge).
    visit_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("visits.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default=PaymentKind.DEPOSIT)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="mpesa")
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    # The part of `amount` that's a tip for the staff member (a checkout charge can include one).
    tip_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KES")
    phone: Mapped[str] = mapped_column(Text, nullable=False)  # 2547XXXXXXXX as sent to M-Pesa

    # Where the money was routed (copied from the business's payment destination at the time).
    destination_type: Mapped[str] = mapped_column(String(20), nullable=False)
    party_b: Mapped[str] = mapped_column(String(20), nullable=False)
    account_reference: Mapped[str] = mapped_column(String(20), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=PaymentStatus.PENDING)
    # cancelled | insufficient_funds | wrong_pin | timeout | busy | failed | provider_error
    failure_reason: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # Daraja identifiers and result. checkout_request_id is null if the push itself failed.
    checkout_request_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, unique=True)
    merchant_request_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    mpesa_receipt: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    result_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_callback: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_payments_booking_created", "booking_id", "created_at"),
        Index("ix_payments_status_created", "status", "created_at"),  # reconciliation scans pending ones
        Index("ix_payments_visit_created", "visit_id", "created_at"),
    )


class PaymentAllocation(Base):
    """One booking's share of a visit payment: its balance, plus the tip for its staff member
    (each person's tip sits on their first booking in the visit)."""

    __tablename__ = "payment_allocations"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    payment_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    booking_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    staff_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, nullable=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # the balance part
    tip_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
