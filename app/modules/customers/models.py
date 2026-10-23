"""A business's customers (fulfillment.json#save-customer / #list-customers)."""
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.db_types import UUID, uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BusinessCustomer(Base):
    """One per person per business. Filled automatically from bookings (any source) and
    editable by the owner; deduplicated by phone (E.164) when there is one."""

    __tablename__ = "business_customers"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # E.164, e.g. +254712345678
    email: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_business_customers_business", "business_id"),
        Index(
            "uq_business_customers_phone", "business_id", "phone", unique=True,
            postgresql_where=text("phone IS NOT NULL"), sqlite_where=text("phone IS NOT NULL"),
        ),
    )
