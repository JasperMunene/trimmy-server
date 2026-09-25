from typing import Optional
from fastapi import APIRouter, Depends, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, get_current_verified_user
from app.modules.auth.models import User
from app.modules.business.schemas import (
    BusinessProfileResponse,
    CreateBusinessRequest,
    CreateServiceCategoryRequest,
    CreateServiceRequest,
    InviteTeamMemberRequest,
    ListMyBusinessesResponse,
    ListMyInvitationsResponse,
    PublishBusinessRequest,
    RespondToInvitationRequest,
    RespondToInvitationResponse,
    SearchResponse,
    SetPaymentDestinationRequest,
    SetPoliciesRequest,
    SetTeamModeRequest,
    UpdateServiceRequest,
    WorkingHoursRequest,
)
from app.modules.business.service import BusinessService

router = APIRouter(prefix="/businesses", tags=["Business Setup"])
team_router = APIRouter(tags=["Team"])
discovery_router = APIRouter(prefix="/businesses", tags=["Discovery"])


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


@router.post("", status_code=201)
async def create_business(
    data: CreateBusinessRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.create_business(current_user, data)


@router.get("/{business_id}")
async def get_business(
    business_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.get_business(business_id, str(current_user.id))


@router.patch("/{business_id}")
async def update_business(
    business_id: str,
    data: CreateBusinessRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.update_business(business_id, str(current_user.id), data)


@router.post("/{business_id}/photos", status_code=201)
async def upload_photo(
    business_id: str,
    photo: UploadFile = File(...),
    isCover: str = Form("false"),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    contents = await photo.read()
    url = f"data:{photo.content_type};base64,{len(contents)}_placeholder"
    service = BusinessService(db)
    return await service.upload_photo(business_id, str(current_user.id), url, isCover.lower() == "true")


@router.delete("/{business_id}/photos/{photo_id}", status_code=204)
async def delete_photo(
    business_id: str,
    photo_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    await service.delete_photo(business_id, str(current_user.id), photo_id)


@router.put("/{business_id}/working-hours")
async def set_working_hours(
    business_id: str,
    data: WorkingHoursRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.set_working_hours(business_id, str(current_user.id), data.weekly_hours)


@router.post("/{business_id}/service-categories", status_code=201)
async def create_service_category(
    business_id: str,
    data: CreateServiceCategoryRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.create_service_category(business_id, str(current_user.id), data)


@router.get("/{business_id}/service-categories")
async def list_service_categories(
    business_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.list_service_categories(business_id, str(current_user.id))


@router.post("/{business_id}/services", status_code=201)
async def create_service(
    business_id: str,
    data: CreateServiceRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.create_service(business_id, str(current_user.id), data)


@router.patch("/{business_id}/services/{service_id}")
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


@router.put("/{business_id}/policies")
async def set_policies(
    business_id: str,
    data: SetPoliciesRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.set_policies(business_id, str(current_user.id), data)


@router.put("/{business_id}/payment-destination")
async def set_payment_destination(
    business_id: str,
    data: SetPaymentDestinationRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.set_payment_destination(business_id, str(current_user.id), data)


@router.patch("/{business_id}/team-mode")
async def set_team_mode(
    business_id: str,
    data: SetTeamModeRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.set_team_mode(business_id, str(current_user.id), data)


@router.post("/{business_id}/team/invitations", status_code=201)
async def invite_team_member(
    business_id: str,
    data: InviteTeamMemberRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    service = BusinessService(db)
    return await service.invite_team_member(business_id, str(current_user.id), data)


@router.patch("/{business_id}/publish")
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
