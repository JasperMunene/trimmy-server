import uuid as uuid_pkg
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.booking.schemas import DaySlotsResponse, DaysResponse
from app.modules.booking.service import AvailabilityService

availability_router = APIRouter(prefix="/businesses", tags=["Booking"])

_SERVICE_IDS = Query(..., alias="serviceIds", description="Repeat for each service: ?serviceIds=a&serviceIds=b")


@availability_router.get("/{business_id}/availability/days", response_model=DaysResponse)
async def availability_days(
    business_id: str,
    service_ids: List[uuid_pkg.UUID] = _SERVICE_IDS,
    staff_id: Optional[uuid_pkg.UUID] = Query(None, alias="staffId"),
    start: Optional[date] = Query(None, alias="from", description="First day (business local); default today"),
    days: int = Query(14, description="How many days, 1-31"),
    db: AsyncSession = Depends(get_db),
):
    """Which days are open and have at least one slot, for the date strip. Public."""
    return await AvailabilityService(db).days(business_id, start, days, service_ids, staff_id)


@availability_router.get("/{business_id}/availability", response_model=DaySlotsResponse)
async def availability_for_day(
    business_id: str,
    day: date = Query(..., alias="date"),
    service_ids: List[uuid_pkg.UUID] = _SERVICE_IDS,
    staff_id: Optional[uuid_pkg.UUID] = Query(None, alias="staffId"),
    db: AsyncSession = Depends(get_db),
):
    """Bookable start times on one day for these services. Omit staffId for "any available". Public."""
    return await AvailabilityService(db).day(business_id, day, service_ids, staff_id)
