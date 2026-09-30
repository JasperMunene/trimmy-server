from typing import Optional
from fastapi import APIRouter, Depends, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, get_current_verified_user
from app.modules.auth.models import User
from app.modules.business import payment_options
from app.modules.business.schemas import (
    AddTeamMemberRequest,
    BookingSettingsResponse,
    BusinessProfileResponse,
    CompletePhotoUploadRequest,
    CreateBusinessRequest,
    CreateBusinessResponse,
    CreateServiceCategoryRequest,
    CreateServiceRequest,
    GetBusinessResponse,
    InviteTeamMemberRequest,
    ListMyBusinessesResponse,
    MyStaffServicesResponse,
    ListMyInvitationsResponse,
    ListServiceCategoriesResponse,
    ListTeamMembersResponse,
    PaymentDestinationOptionsResponse,
    PaymentDestinationResponse,
    PhotoResponse,
    PhotoUploadUrlRequest,
    PhotoUploadUrlResponse,
    PoliciesResponse,
    PublishBusinessRequest,
    PublishBusinessResponse,
    RespondToInvitationRequest,
    RespondToInvitationResponse,
    SearchResponse,
    ServiceCategoryResponse,
    ServiceResponse,
    SetPaymentDestinationRequest,
    SetPoliciesRequest,
    SetStaffServicesRequest,
    SetTeamModeRequest,
    StaffServicesResponse,
    TeamMemberResponse,
    TeamModeResponse,
    UpdateBookingSettingsRequest,
    UpdateBusinessResponse,
    UpdateServiceRequest,
    UpdateTeamMemberRequest,
    WorkingHoursRequest,
    WorkingHoursResponse,
)
from app.modules.business.service import BusinessService

router = APIRouter(prefix="/businesses", tags=["Business Setup"])
team_router = APIRouter(tags=["Team"])
discovery_router = APIRouter(prefix="/businesses", tags=["Discovery"])
options_router = APIRouter(tags=["Business Setup"])


@options_router.get("/payment-destinations/options", response_model=PaymentDestinationOptionsResponse)
async def payment_destination_options():
    """Choices for the Payment Destination screen: the three types with their
    fields, and banks with their M-Pesa paybill shortcodes. Public, static."""
    return {"types": payment_options.TYPES, "banks": payment_options.BANKS}


# ─── Discovery (public) ────────────────────────────────────────
# Registered on its own router so main.py can mount it BEFORE `router`;
# otherwise GET /businesses/search would be swallowed by GET /businesses/{id}.


@discovery_router.get("/search", response_model=SearchResponse)
async def search_businesses(
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    query: Optional[str] = None,
    category: Optional[str] = None,
    priceMax: Optional[int] = None,
    ratingMin: Optional[float] = None,
    radiusKm: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.search_businesses(
        lat=lat,
        lng=lng,
        query=query,
        category=category,
        price_max=priceMax,
        rating_min=ratingMin,
        radius_km=radiusKm,
    )


@discovery_router.get("/{business_id}/profile", response_model=BusinessProfileResponse)
async def get_business_profile(
    business_id: str,
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.get_business_profile(business_id)


@router.post("", status_code=201, response_model=CreateBusinessResponse)
async def create_business(
    data: CreateBusinessRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.create_business(current_user, data)


@router.get("/{business_id}", response_model=GetBusinessResponse)
async def get_business(
    business_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.get_business(business_id, str(current_user.id))


@router.patch("/{business_id}", response_model=UpdateBusinessResponse)
async def update_business(
    business_id: str,
    data: CreateBusinessRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.update_business(business_id, str(current_user.id), data)


@router.post("/{business_id}/photos/upload-url", response_model=PhotoUploadUrlResponse)
async def create_photo_upload_url(
    business_id: str,
    data: PhotoUploadUrlRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Step 1 of a direct upload: get a presigned URL, then PUT the file to it
    with exactly the returned headers, then call POST /photos/complete."""
    service = BusinessService(db)
    return await service.create_photo_upload_url(business_id, str(current_user.id), data)


@router.post("/{business_id}/photos/complete", status_code=201, response_model=PhotoResponse)
async def complete_photo_upload(
    business_id: str,
    data: CompletePhotoUploadRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Step 2 of a direct upload: record the uploaded file as a business photo."""
    service = BusinessService(db)
    return await service.complete_photo_upload(business_id, str(current_user.id), data)


@router.post("/{business_id}/photos", status_code=201, response_model=PhotoResponse)
async def upload_photo(
    business_id: str,
    photo: UploadFile = File(...),
    isCover: str = Form("false"),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload through the API (multipart). Prefer the direct upload-url flow."""
    service = BusinessService(db)
    return await service.upload_photo(business_id, str(current_user.id), photo, isCover.lower() == "true")


@router.delete("/{business_id}/photos/{photo_id}", status_code=204)
async def delete_photo(
    business_id: str,
    photo_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    await service.delete_photo(business_id, str(current_user.id), photo_id)


@router.put("/{business_id}/working-hours", response_model=WorkingHoursResponse)
async def set_working_hours(
    business_id: str,
    data: WorkingHoursRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.set_working_hours(business_id, str(current_user.id), data.weekly_hours)


@router.post("/{business_id}/service-categories", status_code=201, response_model=ServiceCategoryResponse)
async def create_service_category(
    business_id: str,
    data: CreateServiceCategoryRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.create_service_category(business_id, str(current_user.id), data)


@router.get("/{business_id}/service-categories", response_model=ListServiceCategoriesResponse)
async def list_service_categories(
    business_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.list_service_categories(business_id, str(current_user.id))


@router.post("/{business_id}/services", status_code=201, response_model=ServiceResponse)
async def create_service(
    business_id: str,
    data: CreateServiceRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.create_service(business_id, str(current_user.id), data)


@router.patch("/{business_id}/services/{service_id}", response_model=ServiceResponse)
async def update_service(
    business_id: str,
    service_id: str,
    data: UpdateServiceRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.update_service(business_id, str(current_user.id), service_id, data)


@router.delete("/{business_id}/services/{service_id}", status_code=204)
async def delete_service(
    business_id: str,
    service_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    await service.delete_service(business_id, str(current_user.id), service_id)


@router.delete("/{business_id}/service-categories/{category_id}", status_code=204)
async def delete_service_category(
    business_id: str,
    category_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    await service.delete_service_category(business_id, str(current_user.id), category_id)


@router.put("/{business_id}/policies", response_model=PoliciesResponse)
async def set_policies(
    business_id: str,
    data: SetPoliciesRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.set_policies(business_id, str(current_user.id), data)


@router.put("/{business_id}/payment-destination", response_model=PaymentDestinationResponse)
async def set_payment_destination(
    business_id: str,
    data: SetPaymentDestinationRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.set_payment_destination(business_id, str(current_user.id), data)


@router.patch("/{business_id}/team-mode", response_model=TeamModeResponse)
async def set_team_mode(
    business_id: str,
    data: SetTeamModeRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.set_team_mode(business_id, str(current_user.id), data)


@router.post("/{business_id}/team/invitations", status_code=201, response_model=TeamMemberResponse)
async def invite_team_member(
    business_id: str,
    data: InviteTeamMemberRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.invite_team_member(business_id, str(current_user.id), data)


@router.patch("/{business_id}/booking-settings", response_model=BookingSettingsResponse)
async def update_booking_settings(
    business_id: str,
    data: UpdateBookingSettingsRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Timezone and slot rules used by availability. Current values are in GET /businesses/{id}."""
    service = BusinessService(db)
    return await service.update_booking_settings(business_id, str(current_user.id), data)


@router.get("/{business_id}/staff/{staff_id}/services", response_model=StaffServicesResponse)
async def get_staff_services(
    business_id: str,
    staff_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.get_staff_services(business_id, str(current_user.id), staff_id)


@router.put("/{business_id}/staff/{staff_id}/services", response_model=StaffServicesResponse)
async def set_staff_services(
    business_id: str,
    staff_id: str,
    data: SetStaffServicesRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Which services this staff member performs (drives availability and booking)."""
    service = BusinessService(db)
    return await service.set_staff_services(business_id, str(current_user.id), staff_id, data)


@router.get("/{business_id}/team/members", response_model=ListTeamMembersResponse)
async def list_team_members(
    business_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.list_team_members(business_id, str(current_user.id))


@router.post("/{business_id}/team/members", status_code=201, response_model=TeamMemberResponse)
async def add_team_member(
    business_id: str,
    data: AddTeamMemberRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.add_team_member(business_id, str(current_user.id), data)


@router.patch("/{business_id}/team/members/{invitation_id}", response_model=TeamMemberResponse)
async def update_team_member(
    business_id: str,
    invitation_id: str,
    data: UpdateTeamMemberRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.update_team_member(business_id, str(current_user.id), invitation_id, data)


@router.delete("/{business_id}/team/members/{invitation_id}", status_code=204)
async def remove_team_member(
    business_id: str,
    invitation_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    await service.remove_team_member(business_id, str(current_user.id), invitation_id)


@router.patch("/{business_id}/publish", response_model=PublishBusinessResponse)
async def publish_business(
    business_id: str,
    data: PublishBusinessRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.publish_business(business_id, str(current_user.id), data)


# ─── My Businesses ─────────────────────────────────────────────


@team_router.get("/me/businesses", response_model=ListMyBusinessesResponse)
async def list_my_businesses(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.list_my_businesses(current_user.id)


@team_router.get("/me/staff-services", response_model=MyStaffServicesResponse)
async def my_staff_services(
    business_id: str = Query(..., alias="businessId"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Staff / front desk: the services I've been assigned at this business."""
    service = BusinessService(db)
    return await service.my_staff_services(current_user.id, business_id)


# ─── Team Invitations (received) ──────────────────────────────


@team_router.get("/me/invitations", response_model=ListMyInvitationsResponse)
async def list_my_invitations(
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.list_my_invitations(str(current_user.id))


@team_router.post("/invitations/{invitation_id}/respond", response_model=RespondToInvitationResponse)
async def respond_to_invitation(
    invitation_id: str,
    data: RespondToInvitationRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.respond_to_invitation(invitation_id, str(current_user.id), data)
