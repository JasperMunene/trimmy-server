from pydantic import BaseModel, Field
from typing import Optional, List
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
    service_categories: Optional[List["ServiceCategoryResponse"]] = Field([], alias="serviceCategories")
    policies: Optional["PoliciesResponse"] = None
    payment_destination: Optional["PaymentDestinationResponse"] = Field(None, alias="paymentDestination")
    team_mode: Optional[str] = Field(None, alias="teamMode")
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

class DayHoursResponse(BaseModel):
    open: str
    close: str


class WorkingHoursRequest(BaseModel):
    weekly_hours: dict = Field(alias="weeklyHours")

    model_config = {"populate_by_name": True}


class WorkingHoursResponse(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    weekly_hours: dict = Field(alias="weeklyHours")
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


class ListServiceCategoriesResponse(BaseModel):
    categories: List[dict]


# ─── Policies ────────────────────────────────────────────────

class DepositRuleRequest(BaseModel):
    required: bool
    type: str = Field(..., pattern=r"^(fixed|percent)$")
    amount: MoneyResponse
    applies_to: str = Field(alias="appliesTo", pattern=r"^(all_services|selected_services)$")

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
    deposit: dict
    cancellation: dict
    no_show: dict = Field(alias="noShow")
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


class TeamInvitationResponse(BaseModel):
    invitation_id: uuid_pkg.UUID = Field(alias="invitationId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    email: Optional[str] = None
    phone: Optional[str] = None
    role: str
    status: str
    sent_at: datetime = Field(alias="sentAt")

    model_config = {"from_attributes": True, "populate_by_name": True}


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
    amount: MoneyResponse
    applies_to: Optional[str] = Field(None, alias="appliesTo")

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
    working_hours: dict = Field(alias="workingHours")
    policies: ProfilePoliciesResponse
    amenities: List[str]
    services: List[ProfileServiceResponse]
    staff: List[StaffMemberResponse]
    reviews: List[ReviewResponse]

    model_config = {"populate_by_name": True}
