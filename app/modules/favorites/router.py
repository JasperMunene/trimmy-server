"""Favorites (discovery.json#favorites): businesses and staff a customer saved, kept on the server."""
import uuid as uuid_pkg
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.auth.models import User
from app.modules.business.models import Business, BusinessStatus, ServiceCategory, Staff
from app.modules.business.schemas import BusinessSummaryResponse
from app.modules.business.service import business_summary
from app.modules.favorites.models import FavoriteBusiness, FavoriteStaff

router = APIRouter(prefix="/me/favorites", tags=["Favorites"])


class FavoriteStaffResponse(BaseModel):
    staff_id: uuid_pkg.UUID = Field(alias="staffId")
    name: str
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    business_name: str = Field(alias="businessName")
    avatar_url: Optional[str] = Field(None, alias="avatarUrl")
    role: str
    rating: float

    model_config = {"populate_by_name": True}


class FavoritesResponse(BaseModel):
    businesses: List[BusinessSummaryResponse]
    staff: List[FavoriteStaffResponse]


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "not_found", "message": f"No published {what} found for this id"})


def _uuid(raw: str, what: str) -> uuid_pkg.UUID:
    try:
        return uuid_pkg.UUID(raw)
    except ValueError:
        raise _not_found(what)


@router.get("", response_model=FavoritesResponse)
async def list_favorites(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Newest saved first. Businesses that are no longer published (and their staff) are left out."""
    businesses = (
        await db.execute(
            select(Business)
            .join(FavoriteBusiness, FavoriteBusiness.business_id == Business.id)
            .where(FavoriteBusiness.user_id == current_user.id, Business.status == BusinessStatus.PUBLISHED)
            .order_by(FavoriteBusiness.created_at.desc())
            .options(
                selectinload(Business.photos),
                selectinload(Business.service_categories).selectinload(ServiceCategory.services),
                selectinload(Business.reviews),
            )
        )
    ).scalars().all()
    staff = (
        await db.execute(
            select(Staff, Business.name)
            .join(FavoriteStaff, FavoriteStaff.staff_id == Staff.id)
            .join(Business, Business.id == Staff.business_id)
            .where(FavoriteStaff.user_id == current_user.id, Business.status == BusinessStatus.PUBLISHED,
                   Staff.role != "front_desk")
            .order_by(FavoriteStaff.created_at.desc())
        )
    ).all()
    return FavoritesResponse(
        businesses=[business_summary(b) for b in businesses],
        staff=[
            FavoriteStaffResponse(staffId=s.id, name=s.name, businessId=s.business_id, businessName=name,
                                  avatarUrl=s.avatar_url, role=s.role, rating=s.rating or 0.0)
            for s, name in staff
        ],
    )


@router.put("/businesses/{business_id}", status_code=status.HTTP_204_NO_CONTENT)
async def save_business(business_id: str, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Idempotent."""
    bid = _uuid(business_id, "business")
    if (await db.execute(select(Business.id).where(Business.id == bid, Business.status == BusinessStatus.PUBLISHED))).first() is None:
        raise _not_found("business")
    if await db.get(FavoriteBusiness, (current_user.id, bid)) is None:
        db.add(FavoriteBusiness(user_id=current_user.id, business_id=bid))
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/businesses/{business_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_business(business_id: str, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Idempotent."""
    await db.execute(delete(FavoriteBusiness).where(
        FavoriteBusiness.user_id == current_user.id, FavoriteBusiness.business_id == _uuid(business_id, "business")))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/staff/{staff_id}", status_code=status.HTTP_204_NO_CONTENT)
async def save_staff(staff_id: str, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Idempotent."""
    sid = _uuid(staff_id, "staff member")
    found = await db.execute(
        select(Staff.id).join(Business, Business.id == Staff.business_id)
        .where(Staff.id == sid, Business.status == BusinessStatus.PUBLISHED, Staff.role != "front_desk")
    )
    if found.first() is None:
        raise _not_found("staff member")
    if await db.get(FavoriteStaff, (current_user.id, sid)) is None:
        db.add(FavoriteStaff(user_id=current_user.id, staff_id=sid))
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/staff/{staff_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_staff(staff_id: str, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Idempotent."""
    await db.execute(delete(FavoriteStaff).where(
        FavoriteStaff.user_id == current_user.id, FavoriteStaff.staff_id == _uuid(staff_id, "staff member")))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
