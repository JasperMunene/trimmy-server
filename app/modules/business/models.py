from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, Integer, Float, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
import uuid as uuid_pkg
from app.core.base import Base
from app.core.db_types import UUID, uuid4


class BusinessStatus(str, PyEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class OnboardingStep(str, PyEnum):
    BUSINESS_BASICS = "business_basics"
    PHOTOS = "photos"
    WORKING_HOURS = "working_hours"
    SERVICES = "services"
    POLICIES = "policies"
    PAYMENT_DESTINATION = "payment_destination"
    TEAM_MODE = "team_mode"
    REVIEW_PUBLISH = "review_publish"


class TeamMode(str, PyEnum):
    SOLO = "solo"
    TEAM = "team"


class InvitationStatus(str, PyEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class PaymentDestinationType(str, PyEnum):
    MPESA_TILL = "mpesa_till"
    MPESA_PAYBILL = "mpesa_paybill"
    BANK_ACCOUNT = "bank_account"


class VerificationStatus(str, PyEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    owner_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    categories: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    location: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=BusinessStatus.DRAFT, index=True)
    onboarding_step: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    team_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    photos: Mapped[list["BusinessPhoto"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    working_hours: Mapped[Optional["WorkingHours"]] = relationship(
        back_populates="business", uselist=False, cascade="all, delete-orphan"
    )
    service_categories: Mapped[list["ServiceCategory"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    policies: Mapped[Optional["BusinessPolicies"]] = relationship(
        back_populates="business", uselist=False, cascade="all, delete-orphan"
    )
    payment_destination: Mapped[Optional["PaymentDestination"]] = relationship(
        back_populates="business", uselist=False, cascade="all, delete-orphan"
    )
    team_invitations: Mapped[list["TeamInvitation"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_businesses_owner_status", "owner_id", "status"),
    )


class BusinessPhoto(Base):
    __tablename__ = "business_photos"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    is_cover: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    business: Mapped["Business"] = relationship(back_populates="photos")


class WorkingHours(Base):
    __tablename__ = "working_hours"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    weekly_hours: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    business: Mapped["Business"] = relationship(back_populates="working_hours")


class ServiceCategory(Base):
    __tablename__ = "service_categories"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    business: Mapped["Business"] = relationship(back_populates="service_categories")
    services: Mapped[list["Service"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class Service(Base):
    __tablename__ = "services"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("service_categories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    price_currency: Mapped[str] = mapped_column(String(3), default="KES")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    category: Mapped["ServiceCategory"] = relationship(back_populates="services")


class BusinessPolicies(Base):
    __tablename__ = "business_policies"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    deposit_required: Mapped[bool] = mapped_column(Boolean, default=False)
    deposit_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    deposit_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deposit_currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    deposit_applies_to: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    cancellation_free_hours: Mapped[int] = mapped_column(Integer, default=24)
    cancellation_late_fee_percent: Mapped[int] = mapped_column(Integer, default=50)
    no_show_fee_percent: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    business: Mapped["Business"] = relationship(back_populates="policies")


class PaymentDestination(Base):
    __tablename__ = "payment_destinations"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    till_number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paybill_number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paybill_account_number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bank_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bank_shortcode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bank_account_number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(20), default=VerificationStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    business: Mapped["Business"] = relationship(back_populates="payment_destination")


class TeamInvitation(Base):
    __tablename__ = "team_invitations"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=InvitationStatus.PENDING)
    invited_by: Mapped[uuid_pkg.UUID] = mapped_column(UUID, nullable=False)
    user_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    business: Mapped["Business"] = relationship(back_populates="team_invitations")
