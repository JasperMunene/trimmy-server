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
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    amenities: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    categories: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    location: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=BusinessStatus.DRAFT, index=True)
    onboarding_step: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    team_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Suspended by Trimmy's team (trust & safety): hidden from customers, no new online bookings;
    # the owner's own calendar, walk-ins and checkout keep working. Status stays as it was.
    suspended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Booking settings (docs/booking/PLAN.md, D5 and D8). Working hours are in this timezone.
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Africa/Nairobi")
    slot_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_lead_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    max_days_ahead: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
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
    staff: Mapped[list["Staff"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["Review"]] = relationship(
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
    # Key in object storage (see app/core/storage.py); null for rows from before real storage.
    storage_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    # Money deposit (type "fixed").
    deposit_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deposit_currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    # Percentage deposit (type "percent"), 0-100.
    deposit_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deposit_applies_to: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # Service ids (as strings) the deposit applies to when applies_to = "selected_services".
    deposit_service_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
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
    # When someone on Trimmy's team checked these details (null: not yet, or changed since).
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, nullable=True)
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
    # The invitation doubles as the team roster record, so member details live here.
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Share of each service they perform; null for desk-only members (they perform none).
    commission_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # May also run the front desk (role 'staff' with this on = both modes; role 'front_desk' always on).
    front_desk_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Lowercase weekday names, or null for "same as business hours".
    working_days: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    invited_by: Mapped[uuid_pkg.UUID] = mapped_column(UUID, nullable=False)
    user_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, nullable=True)
    # Set when the invite is accepted: the Staff row that is this member's bookable identity.
    staff_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("staff.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    business: Mapped["Business"] = relationship(back_populates="team_invitations")


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bio: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    specialties: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    portfolio: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Lowercase weekday names this person works, or null for "whenever the business is open".
    # Copied from the invitation on accept; Staff is the source of truth from then on.
    working_days: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    commission_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # May run the front desk: manage bookings, check customers out. role 'front_desk' = desk only
    # (not bookable, no commission); role 'staff' with this on = one person, two modes.
    front_desk_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # True: performs every service (the default). False: only the services in staff_services,
    # which may be none (e.g. front desk). An explicit flag, so deleting a staff member's last
    # mapped service can't silently turn them into "performs everything".
    performs_all_services: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    business: Mapped["Business"] = relationship(back_populates="staff")


class StaffPortfolioPhoto(Base):
    """A photo of a staff member's past work, shown on their profile. Uploaded by the
    member themselves or the owner; its own table so concurrent uploads can't overwrite
    each other (Staff.portfolio is the legacy JSON list, kept for old rows)."""

    __tablename__ = "staff_portfolio_photos"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    staff_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("staff.id", ondelete="CASCADE"), nullable=False, index=True
    )
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class StaffService(Base):
    """A service a staff member performs (only consulted when performs_all_services is False)."""

    __tablename__ = "staff_services"

    staff_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("services.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    business_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    staff_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("staff.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The booking it's about (one review per booking) and who wrote it; null for older rows.
    booking_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    customer_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # The business's public reply (owner or front desk), shown under the review.
    reply_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_by: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(UUID, nullable=True)
    # Hidden by Trimmy's team (e.g. fake or abusive): left out of profiles, lists and ratings.
    hidden_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    hidden_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    business: Mapped["Business"] = relationship(back_populates="reviews")
