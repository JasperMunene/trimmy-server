from pydantic import BaseModel, Field
from typing import Literal, Optional, List
from datetime import datetime
import uuid as uuid_pkg


# ─── Location ────────────────────────────────────────────────

class LocationRequest(BaseModel):
    address: str
    lat: float
    lng: float
    building: Optional[str] = None
    floor: Optional[str] = None
    unit: Optional[str] = None


class LocationResponse(BaseModel):
    address: str
    lat: float
    lng: float
    building: Optional[str] = None
    floor: Optional[str] = None
    unit: Optional[str] = None

    model_config = {"from_attributes": True}


# ─── Money ───────────────────────────────────────────────────

class MoneyResponse(BaseModel):
    amount: int
    currency: str = "KES"


# ─── Create Business ────────────────────────────────────────

class CreateBusinessRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    categories: List[str] = Field(..., min_length=1)
    phone: str = Field(..., min_length=1)
    location: LocationRequest
    description: Optional[str] = None
    amenities: Optional[List[str]] = None


class CreateBusinessResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    name: str
    description: Optional[str] = None
    categories: List[str]
    phone: str
    location: LocationResponse
    status: str
    onboarding_step: Optional[str] = Field(None, alias="onboardingStep")
    created_at: datetime = Field(alias="createdAt")

    model_config = {"from_attributes": True, "populate_by_name": True}


class UpdateBusinessResponse(BaseModel):
    name: str
    description: Optional[str] = None
    categories: List[str]
    phone: str
    location: LocationResponse

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Get Business Draft ─────────────────────────────────────

class GetBusinessResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    name: str
    categories: Optional[List[str]] = None
    phone: str
    description: Optional[str] = None
    amenities: Optional[List[str]] = None
    location: Optional[LocationResponse] = None
    photos: Optional[List["PhotoResponse"]] = []
    working_hours: Optional["WorkingHoursResponse"] = Field(None, alias="workingHours")
    service_categories: List["ServiceCategoryWithServicesResponse"] = Field([], alias="serviceCategories")
    policies: Optional["PoliciesResponse"] = None
    payment_destination: Optional["PaymentDestinationResponse"] = Field(None, alias="paymentDestination")
    team_mode: Optional[str] = Field(None, alias="teamMode")
    booking_settings: Optional["BookingSettingsResponse"] = Field(None, alias="bookingSettings")
    status: str
    onboarding_step: Optional[str] = Field(None, alias="onboardingStep")

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Photo ───────────────────────────────────────────────────

class PhotoResponse(BaseModel):
    photo_id: uuid_pkg.UUID = Field(alias="photoId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    url: str
    is_cover: bool = Field(alias="isCover")
    created_at: datetime = Field(alias="createdAt")

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Working Hours ───────────────────────────────────────────

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
# Close may also be 24:00 (open until midnight). Overnight hours aren't supported.
_HHMM_CLOSE = r"^(([01]\d|2[0-3]):[0-5]\d|24:00)$"


class DayHoursRequest(BaseModel):
    open: str = Field(..., pattern=_HHMM, examples=["09:00"])
    close: str = Field(..., pattern=_HHMM_CLOSE, examples=["19:00"])

    model_config = {"extra": "forbid"}


class WeeklyHoursRequest(BaseModel):
    """Omitted or null days are closed."""

    monday: Optional[DayHoursRequest] = None
    tuesday: Optional[DayHoursRequest] = None
    wednesday: Optional[DayHoursRequest] = None
    thursday: Optional[DayHoursRequest] = None
    friday: Optional[DayHoursRequest] = None
    saturday: Optional[DayHoursRequest] = None
    sunday: Optional[DayHoursRequest] = None

    model_config = {"extra": "forbid"}


class DayHoursResponse(BaseModel):
    open: str
    close: str


class WeeklyHoursResponse(BaseModel):
    monday: Optional[DayHoursResponse] = None
    tuesday: Optional[DayHoursResponse] = None
    wednesday: Optional[DayHoursResponse] = None
    thursday: Optional[DayHoursResponse] = None
    friday: Optional[DayHoursResponse] = None
    saturday: Optional[DayHoursResponse] = None
    sunday: Optional[DayHoursResponse] = None


class WorkingHoursRequest(BaseModel):
    weekly_hours: WeeklyHoursRequest = Field(alias="weeklyHours")

    model_config = {"populate_by_name": True}


class WorkingHoursResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    weekly_hours: WeeklyHoursResponse = Field(alias="weeklyHours")
    onboarding_step: Optional[str] = Field(None, alias="onboardingStep")

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Service Category ────────────────────────────────────────

class CreateServiceCategoryRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class ServiceCategoryResponse(BaseModel):
    category_id: uuid_pkg.UUID = Field(alias="categoryId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    name: str
    created_at: Optional[datetime] = Field(None, alias="createdAt")

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Service ─────────────────────────────────────────────────

class CreateServiceRequest(BaseModel):
    category_id: uuid_pkg.UUID = Field(alias="categoryId")
    name: str = Field(..., min_length=1, max_length=100)
    duration_minutes: int = Field(alias="durationMinutes", gt=0)
    price: MoneyResponse


class UpdateServiceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    duration_minutes: int = Field(alias="durationMinutes", gt=0)
    price: MoneyResponse

    model_config = {"populate_by_name": True}


class ServiceResponse(BaseModel):
    service_id: uuid_pkg.UUID = Field(alias="serviceId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    category_id: uuid_pkg.UUID = Field(alias="categoryId")
    name: str
    duration_minutes: int = Field(alias="durationMinutes")
    price: MoneyResponse
    created_at: Optional[datetime] = Field(None, alias="createdAt")

    model_config = {"from_attributes": True, "populate_by_name": True}


class ServiceCategoryWithServicesResponse(ServiceCategoryResponse):
    services: List[ServiceResponse] = []


class ListServiceCategoriesResponse(BaseModel):
    categories: List[ServiceCategoryWithServicesResponse]


# ─── Policies ────────────────────────────────────────────────

class DepositRuleRequest(BaseModel):
    required: bool
    type: str = Field(..., pattern=r"^(fixed|percent)$")
    # type "fixed": the deposit as money. Required (amount > 0) when required = true.
    amount: Optional[MoneyResponse] = None
    # type "percent": percentage of the service price, 1-100. Required when required = true.
    percent: Optional[int] = Field(None, ge=0, le=100)
    applies_to: str = Field(alias="appliesTo", pattern=r"^(all_services|selected_services)$")
    # Required (non-empty) when appliesTo = selected_services; must be this business's services.
    service_ids: Optional[List[uuid_pkg.UUID]] = Field(None, alias="serviceIds")

    model_config = {"populate_by_name": True}


class CancellationPolicyRequest(BaseModel):
    free_cancellation_hours: int = Field(alias="freeCancellationHours", ge=0)
    late_fee_percent: int = Field(alias="lateFeePercent", ge=0, le=100)

    model_config = {"populate_by_name": True}


class NoShowPolicyRequest(BaseModel):
    fee_percent: int = Field(alias="feePercent", ge=0, le=100)

    model_config = {"populate_by_name": True}


class SetPoliciesRequest(BaseModel):
    deposit: DepositRuleRequest
    cancellation: CancellationPolicyRequest
    no_show: NoShowPolicyRequest = Field(alias="noShow")

    model_config = {"populate_by_name": True}


class PoliciesResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    deposit: "DepositRuleResponse"
    cancellation: "CancellationPolicyResponse"
    no_show: "NoShowPolicyResponse" = Field(alias="noShow")
    onboarding_step: Optional[str] = Field(None, alias="onboardingStep")

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Payment Destination ─────────────────────────────────────

class SetPaymentDestinationRequest(BaseModel):
    type: str = Field(..., pattern=r"^(mpesa_till|mpesa_paybill|bank_account)$")
    till_number: Optional[str] = Field(None, alias="tillNumber")
    paybill_number: Optional[str] = Field(None, alias="paybillNumber")
    paybill_account_number: Optional[str] = Field(None, alias="paybillAccountNumber")
    bank_name: Optional[str] = Field(None, alias="bankName")
    bank_shortcode: Optional[str] = Field(None, alias="bankShortcode")
    bank_account_number: Optional[str] = Field(None, alias="bankAccountNumber")

    model_config = {"populate_by_name": True}


class PaymentDestinationResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    type: str
    till_number: Optional[str] = Field(None, alias="tillNumber")
    paybill_number: Optional[str] = Field(None, alias="paybillNumber")
    paybill_account_number: Optional[str] = Field(None, alias="paybillAccountNumber")
    bank_name: Optional[str] = Field(None, alias="bankName")
    bank_shortcode: Optional[str] = Field(None, alias="bankShortcode")
    bank_account_number: Optional[str] = Field(None, alias="bankAccountNumber")
    verification_status: str = Field(alias="verificationStatus")
    onboarding_step: Optional[str] = Field(None, alias="onboardingStep")

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Team Mode ───────────────────────────────────────────────

class SetTeamModeRequest(BaseModel):
    team_mode: str = Field(alias="teamMode", pattern=r"^(solo|team)$")

    model_config = {"populate_by_name": True}


class TeamModeResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    team_mode: str = Field(alias="teamMode")
    onboarding_step: Optional[str] = Field(None, alias="onboardingStep")

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Team Invitation ─────────────────────────────────────────

class InviteTeamMemberRequest(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None
    role: str = Field(..., pattern=r"^(front_desk|staff)$")


class AddTeamMemberRequest(InviteTeamMemberRequest):
    name: str = Field(..., min_length=1, max_length=100)
    commission_percent: int = Field(40, alias="commissionPercent", ge=0, le=100)
    # null = works whenever the business is open.
    working_days: Optional[List[Weekday]] = Field(None, alias="workingDays")

    model_config = {"populate_by_name": True}


class UpdateTeamMemberRequest(BaseModel):
    """Only the fields sent are changed; send workingDays: null to clear it."""

    commission_percent: Optional[int] = Field(None, alias="commissionPercent", ge=0, le=100)
    working_days: Optional[List[Weekday]] = Field(None, alias="workingDays")

    model_config = {"populate_by_name": True}


class TeamMemberResponse(BaseModel):
    """An invitation is the roster record, pending or accepted."""

    invitation_id: uuid_pkg.UUID = Field(alias="invitationId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    # The bookable identity (profile staff[].staffId, bookings). Null until the invite is accepted.
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    role: str
    status: str
    commission_percent: int = Field(alias="commissionPercent")
    working_days: Optional[List[str]] = Field(None, alias="workingDays")
    sent_at: datetime = Field(alias="sentAt")

    model_config = {"from_attributes": True, "populate_by_name": True}


class ListTeamMembersResponse(BaseModel):
    members: List[TeamMemberResponse]


# ─── My Invitations (received) ───────────────────────────────

class MyInvitationResponse(BaseModel):
    invitation_id: uuid_pkg.UUID = Field(alias="invitationId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    business_name: str = Field(alias="businessName")
    role: str
    status: str
    sent_at: datetime = Field(alias="sentAt")

    model_config = {"from_attributes": True, "populate_by_name": True}


class ListMyInvitationsResponse(BaseModel):
    invitations: List[MyInvitationResponse]


# ─── My Businesses ───────────────────────────────────────────

class MyBusinessResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    name: str
    status: str
    role: str

    model_config = {"populate_by_name": True}


class ListMyBusinessesResponse(BaseModel):
    businesses: List[MyBusinessResponse]


# ─── Respond to Invitation ───────────────────────────────────

class RespondToInvitationRequest(BaseModel):
    action: str = Field(..., pattern=r"^(accept|decline)$")


class RespondToInvitationResponse(BaseModel):
    invitation_id: uuid_pkg.UUID = Field(alias="invitationId")
    status: str

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Publish Business ────────────────────────────────────────

class PublishBusinessRequest(BaseModel):
    visible: bool


class PublishBusinessResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    status: str
    visible: bool
    published_at: Optional[datetime] = Field(None, alias="publishedAt")

    model_config = {"from_attributes": True, "populate_by_name": True}


# ─── Discovery: Search ───────────────────────────────────────

class BusinessSummaryResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    name: str
    categories: List[str]
    thumbnail_url: Optional[str] = Field(None, alias="thumbnailUrl")
    photos: List[str]
    address: str
    lat: float
    lng: float
    distance_km: Optional[float] = Field(None, alias="distanceKm")
    starting_price: Optional[MoneyResponse] = Field(None, alias="startingPrice")
    rating: float
    review_count: int = Field(alias="reviewCount")

    model_config = {"populate_by_name": True}


class SearchResponse(BaseModel):
    businesses: List[BusinessSummaryResponse]


# ─── Discovery: Profile ──────────────────────────────────────

class ProfileServiceResponse(BaseModel):
    service_id: uuid_pkg.UUID = Field(alias="serviceId")
    category_name: str = Field(alias="categoryName")
    name: str
    duration_minutes: int = Field(alias="durationMinutes")
    price: MoneyResponse

    model_config = {"populate_by_name": True}


class StaffMemberResponse(BaseModel):
    staff_id: uuid_pkg.UUID = Field(alias="staffId")
    name: str
    role: str
    # Null = works whenever the business is open.
    working_days: Optional[List[str]] = Field(None, alias="workingDays")
    # allServices true = performs every service; otherwise only serviceIds (possibly none).
    all_services: bool = Field(True, alias="allServices")
    service_ids: List[uuid_pkg.UUID] = Field([], alias="serviceIds")
    rating: float
    avatar_url: Optional[str] = Field(None, alias="avatarUrl")
    bio: Optional[str] = None
    specialties: List[str]
    portfolio: List[str]

    model_config = {"populate_by_name": True}


class ReviewResponse(BaseModel):
    review_id: uuid_pkg.UUID = Field(alias="reviewId")
    author_name: str = Field(alias="authorName")
    rating: int
    text: Optional[str] = None
    created_at: datetime = Field(alias="createdAt")

    model_config = {"populate_by_name": True}


class DepositRuleResponse(BaseModel):
    required: bool
    type: Optional[str] = None
    # Set for type "fixed", null for "percent".
    amount: Optional[MoneyResponse] = None
    # Set for type "percent", null for "fixed".
    percent: Optional[int] = None
    applies_to: Optional[str] = Field(None, alias="appliesTo")
    service_ids: List[uuid_pkg.UUID] = Field([], alias="serviceIds")

    model_config = {"populate_by_name": True}


class CancellationPolicyResponse(BaseModel):
    free_cancellation_hours: int = Field(alias="freeCancellationHours")
    late_fee_percent: int = Field(alias="lateFeePercent")

    model_config = {"populate_by_name": True}


class NoShowPolicyResponse(BaseModel):
    fee_percent: int = Field(alias="feePercent")

    model_config = {"populate_by_name": True}


class ProfilePoliciesResponse(BaseModel):
    deposit: DepositRuleResponse
    cancellation: CancellationPolicyResponse
    no_show: NoShowPolicyResponse = Field(alias="noShow")

    model_config = {"populate_by_name": True}


class BusinessProfileResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    name: str
    description: str
    categories: List[str]
    photos: List[str]
    rating: float
    review_count: int = Field(alias="reviewCount")
    address: str
    lat: float
    lng: float
    deposit_summary: str = Field(alias="depositSummary")
    cancellation_summary: str = Field(alias="cancellationSummary")
    working_hours: WeeklyHoursResponse = Field(alias="workingHours")
    policies: ProfilePoliciesResponse
    amenities: List[str]
    services: List[ProfileServiceResponse]
    staff: List[StaffMemberResponse]
    reviews: List[ReviewResponse]

    model_config = {"populate_by_name": True}


# ─── Booking settings ────────────────────────────────────────

class UpdateBookingSettingsRequest(BaseModel):
    """Only the fields sent are changed."""

    timezone: Optional[str] = Field(None, examples=["Africa/Nairobi"])
    # Customers see start times on this grid. Must divide an hour evenly.
    slot_interval_minutes: Optional[int] = Field(None, alias="slotIntervalMinutes", ge=5, le=60)
    # Kept free after each booking (clean-up, turnover).
    buffer_minutes: Optional[int] = Field(None, alias="bufferMinutes", ge=0, le=120)
    # How soon from now the earliest bookable slot is.
    min_lead_minutes: Optional[int] = Field(None, alias="minLeadMinutes", ge=0, le=7 * 24 * 60)
    # How far ahead customers can book.
    max_days_ahead: Optional[int] = Field(None, alias="maxDaysAhead", ge=1, le=365)

    model_config = {"populate_by_name": True}


class BookingSettingsResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    timezone: str
    slot_interval_minutes: int = Field(alias="slotIntervalMinutes")
    buffer_minutes: int = Field(alias="bufferMinutes")
    min_lead_minutes: int = Field(alias="minLeadMinutes")
    max_days_ahead: int = Field(alias="maxDaysAhead")

    model_config = {"populate_by_name": True}


GetBusinessResponse.model_rebuild()
PoliciesResponse.model_rebuild()


# ─── Photo uploads (direct to storage) ───────────────────────

class PhotoUploadUrlRequest(BaseModel):
    content_type: Literal["image/jpeg", "image/png"] = Field(alias="contentType")
    size_bytes: int = Field(alias="sizeBytes", gt=0)

    model_config = {"populate_by_name": True}


class PhotoUploadUrlResponse(BaseModel):
    upload_url: str = Field(alias="uploadUrl")
    method: str = "PUT"
    # Send exactly these headers with the PUT (they're part of the signature).
    headers: dict[str, str]
    key: str
    expires_in: int = Field(alias="expiresIn")

    model_config = {"populate_by_name": True}


class CompletePhotoUploadRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=300)
    is_cover: bool = Field(False, alias="isCover")

    model_config = {"populate_by_name": True}


# ─── Payment destination options ─────────────────────────────

class PaymentFieldOption(BaseModel):
    name: str
    label: str
    keyboard: Literal["number", "default", "picker"]
    pattern: str
    hint: str


class PaymentTypeOption(BaseModel):
    type: Literal["mpesa_till", "mpesa_paybill", "bank_account"]
    label: str
    description: str
    fields: List[PaymentFieldOption]


class BankOption(BaseModel):
    name: str
    shortcode: str


class PaymentDestinationOptionsResponse(BaseModel):
    types: List[PaymentTypeOption]
    banks: List[BankOption]


# ─── Staff services ──────────────────────────────────────────

class SetStaffServicesRequest(BaseModel):
    """allServices: true (and no serviceIds) = performs every service, including ones added later.
    allServices: false = performs exactly serviceIds, which may be empty (e.g. front desk)."""

    all_services: bool = Field(alias="allServices")
    service_ids: List[uuid_pkg.UUID] = Field([], alias="serviceIds")

    model_config = {"populate_by_name": True}


class StaffServicesResponse(BaseModel):
    staff_id: uuid_pkg.UUID = Field(alias="staffId")
    all_services: bool = Field(alias="allServices")
    service_ids: List[uuid_pkg.UUID] = Field(alias="serviceIds")

    model_config = {"populate_by_name": True}
