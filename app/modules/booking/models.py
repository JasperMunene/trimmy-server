"""Booking tables (docs/booking/PLAN.md, section 4.1)."""
import secrets
import uuid as uuid_pkg
from datetime import date, datetime, timezone
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base
from app.core.db_types import UUID, uuid4


class BookingStatus(str, PyEnum):
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"


# Statuses that hold a slot (availability.ACTIVE_STATUSES is the same set).
ACTIVE_STATUSES = (BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED, BookingStatus.IN_PROGRESS)


class RefundStatus(str, PyEnum):
    DUE = "due"
    REFUNDED = "refunded"


class BookingSource(str, PyEnum):
    ONLINE = "online"
    WALK_IN = "walk_in"


class PaymentStatus(str, PyEnum):
    UNPAID = "unpaid"
    DEPOSIT_PAID = "deposit_paid"
    PAID = "paid"


# Unambiguous characters (no 0/O, 1/I/L), so references read well over the phone.
_REFERENCE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_reference() -> str:
    """Short customer-facing code, e.g. "BK7F3AQ9". Fits M-Pesa's 12-character AccountReference."""
    return "BK" + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(6))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    reference: Mapped[str] = mapped_column(String(12), nullable=False, unique=True, default=new_reference)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # Null for walk-ins without an account, or once the customer deletes theirs.
    customer_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Null = unassigned ("any available", D4) until the owner assigns someone,
    # or if the assigned staff member is removed.
    staff_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("staff.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False, default=BookingSource.ONLINE)
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # The booked interval in UTC, plus the local date/time the customer picked.
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    local_time: Mapped[str] = mapped_column(String(5), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Money in whole units of `currency`.
    total_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KES")
    deposit_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # null = no deposit
    payment_status: Mapped[str] = mapped_column(String(20), nullable=False, default=PaymentStatus.UNPAID)

    # Snapshot at booking time, so the business keeps the details if the account changes.
    customer_name: Mapped[str] = mapped_column(Text, nullable=False)
    customer_phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    customer_email: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # pending_payment holds the slot until this moment (D6).
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Cancellation money (BK-60, D10): the fee kept from the deposit, and what the
    # business owes back and has to refund by hand.
    cancellation_fee: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    refund_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    refund_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # due | refunded
    refunded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    refund_reference: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # No-show fee (BK-64): the part taken from the paid deposit, and the rest of the fee
    # the deposit didn't cover (there's no card on file to charge it to).
    no_show_fee: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    no_show_fee_uncollected: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    no_show_fee_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The assigned staff member's commission on this booking, fixed when it's paid
    # (total x their commission percent then), so later rate changes don't rewrite it.
    staff_commission: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # When the balance was settled (payment_status became paid): the commission's date in the wallet.
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when each reminder SMS is claimed for sending (BK-62), so it goes out once;
    # rescheduling clears them for the new time.
    reminder_24h_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reminder_2h_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the confirmation SMS is claimed for sending (BK-40), so it goes out once.
    confirmation_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    lines: Mapped[list["BookingLine"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", order_by="BookingLine.position"
    )
    events: Mapped[list["BookingEvent"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", order_by="BookingEvent.at"
    )

    __table_args__ = (
        Index("ix_bookings_business_starts", "business_id", "starts_at"),
        Index("ix_bookings_staff_starts", "staff_id", "starts_at"),
        Index("ix_bookings_customer_starts", "customer_id", "starts_at"),
        Index("ix_bookings_status_expires", "status", "expires_at"),
        # D9: an assigned staff member can't hold two active bookings at once. Postgres
        # only (needs btree_gist); SQLite dev/test databases rely on the app-level check.
        ExcludeConstraint(
            ("staff_id", "="),
            (func.tstzrange(text("starts_at"), text("ends_at")), "&&"),
            name="bookings_staff_no_overlap",
            using="gist",
            where=text("staff_id IS NOT NULL AND status IN ('pending_payment', 'confirmed', 'in_progress')"),
        ).ddl_if(dialect="postgresql"),
    )


class BookingLine(Base):
    """A service on a booking, snapshotted so editing or deleting the service never rewrites history."""

    __tablename__ = "booking_services"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    booking_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    booking: Mapped["Booking"] = relationship(back_populates="lines")


class BookingEvent(Base):
    """Audit trail: every status change, who made it and why."""

    __tablename__ = "booking_events"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    booking_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # null = created
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    # Null = the system (e.g. hold expiry, M-Pesa callback).
    actor_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    booking: Mapped["Booking"] = relationship(back_populates="events")
