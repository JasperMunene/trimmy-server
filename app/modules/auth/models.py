from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
import uuid as uuid_pkg
from app.core.base import Base
from app.core.db_types import UUID, uuid4


class AuthType(str, PyEnum):
    EMAIL = "email"
    PHONE = "phone"
    GOOGLE = "google"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    email: Mapped[Optional[str]] = mapped_column(Text, unique=True, nullable=True, index=True)
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(Text, unique=True, nullable=True, index=True)
    # Google's stable account id ("sub"); set once the user has signed in with Google.
    google_sub: Mapped[Optional[str]] = mapped_column(Text, unique=True, nullable=True, index=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auth_type: Mapped[AuthType] = mapped_column(String(20), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    auth_tokens: Mapped[list["AuthToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_users_email_verified", "email", "verified"),
        Index("idx_users_auth_type_verified", "auth_type", "verified"),
    )


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    user_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(50), nullable=False, default="verification")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    user: Mapped["User"] = relationship(back_populates="auth_tokens")

    __table_args__ = (
        Index("idx_auth_tokens_user_purpose", "user_id", "purpose"),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    user_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")


class OtpSendLog(Base):
    """One row per OTP email sent (or requested), keyed by the raw email so
    rate limits behave the same whether or not an account exists."""

    __tablename__ = "otp_send_log"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("idx_otp_send_log_email_purpose_created", "email", "purpose", "created_at"),
    )
