"""Support tickets: a conversation between someone and Trimmy's team, from the app's Help
screen (later also WhatsApp and safety reports). See support/router.py (people) and
support/admin.py (the team)."""
import secrets
import uuid as uuid_pkg
from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.db_types import UUID, uuid4

_NUMBER_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_ticket_number() -> str:
    """Short code people can read out, e.g. "HX7K2M" (shown as #HX7K2M)."""
    return "".join(secrets.choice(_NUMBER_ALPHABET) for _ in range(6))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TicketStatus(str, PyEnum):
    NEW = "new"            # nobody on the team has answered yet
    OPEN = "open"          # being worked on / the person wrote back
    WAITING = "waiting"    # answered; waiting on the person
    RESOLVED = "resolved"  # sorted (they can still write back, which reopens it)
    CLOSED = "closed"


class TicketChannel(str, PyEnum):
    IN_APP = "in_app"
    WHATSAPP = "whatsapp"
    REPORT = "report"


class TicketCategory(str, PyEnum):
    BOOKING = "booking"
    PAYMENT = "payment"
    REFUND = "refund"
    ACCOUNT = "account"
    BUSINESS = "business"
    BUG = "bug"
    OTHER = "other"


class TicketPriority(str, PyEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class MessageKind(str, PyEnum):
    CUSTOMER = "customer"  # from the person who asked
    AGENT = "agent"        # Trimmy's reply, seen by the person
    NOTE = "note"          # internal note, team only
    EVENT = "event"        # "Assigned to Amani", "Marked resolved"


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    number: Mapped[str] = mapped_column(String(8), unique=True, nullable=False, default=new_ticket_number)
    requester_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Who asked, as they were then (and for WhatsApp numbers without an account).
    requester_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requester_phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requester_email: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    business_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, ForeignKey("businesses.id", ondelete="SET NULL"), nullable=True)
    booking_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default=TicketChannel.IN_APP)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default=TicketCategory.OTHER)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TicketStatus.NEW)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default=TicketPriority.NORMAL)
    assignee_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    # When the person last wrote: the team's "waiting since" clock while it's new/open.
    last_customer_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_support_tickets_status_last", "status", "last_message_at"),
        Index("ix_support_tickets_assignee_status", "assignee_id", "status"),
        Index("ix_support_tickets_requester", "requester_id", "last_message_at"),
    )


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    ticket_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    author_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    author_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attachment_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachment_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default=TicketChannel.IN_APP)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (Index("ix_ticket_messages_ticket_created", "ticket_id", "created_at"),)


class SupportMacro(Base):
    """A canned reply the team picks from (e.g. "How refunds work")."""
    __tablename__ = "support_macros"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
