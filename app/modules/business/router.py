from fastapi import APIRouter, Depends, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_verified_user
from app.modules.auth.models import User
from app.modules.business.schemas import (
    CreateBusinessRequest,
    CreateServiceCategoryRequest,
    CreateServiceRequest,
    InviteTeamMemberRequest,
    PublishBusinessRequest,
    SetPaymentDestinationRequest,
    SetPoliciesRequest,
    SetTeamModeRequest,
    WorkingHoursRequest,
)
from app.modules.business.service import BusinessService

router = APIRouter(prefix="/businesses", tags=["Business Setup"])


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
