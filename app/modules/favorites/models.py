import uuid as uuid_pkg
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.db_types import UUID


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FavoriteBusiness(Base):
    """A customer's saved business (C1 wishlist)."""

    __tablename__ = "favorite_businesses"

    user_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class FavoriteStaff(Base):
    """A customer's saved staff member."""

    __tablename__ = "favorite_staff"

    user_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    staff_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
