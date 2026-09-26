import uuid as uuid_pkg
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_verified_user
from app.modules.auth.models import User
from app.modules.booking.bookings import BookingsService
from app.modules.booking.schemas import (
    AssignStaffRequest,
    BookingResponse,
    CreateBookingRequest,
    DaySlotsResponse,
    DaysResponse,
    ListBookingsResponse,
)
from app.modules.booking.service import AvailabilityService

availability_router = APIRouter(prefix="/businesses", tags=["Booking"])
bookings_router = APIRouter(prefix="/bookings", tags=["Booking"])
my_bookings_router = APIRouter(tags=["Booking"])

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


@bookings_router.post("", status_code=status.HTTP_201_CREATED, response_model=BookingResponse)
async def create_booking(
    data: CreateBookingRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Book a slot. No deposit: confirmed at once. Deposit due: pending_payment, holding the
    slot until expiresAt (pay via POST /bookings/{id}/payments, BK-31)."""
    return await BookingsService(db).create(current_user, data)


@bookings_router.get("/{booking_id}", response_model=BookingResponse)
async def get_booking(
    booking_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """For the customer who booked, and the business's owner and staff. Poll this while paying."""
    return await BookingsService(db).get(current_user, booking_id)


@bookings_router.patch("/{booking_id}/staff", response_model=BookingResponse)
async def assign_booking_staff(
    booking_id: str,
    data: AssignStaffRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner or front desk: assign an "any available" booking to someone, or reassign it."""
    return await BookingsService(db).assign_staff(current_user, booking_id, data.staff_id)


@my_bookings_router.get("/me/bookings", response_model=ListBookingsResponse)
async def list_my_bookings(
    filter: str = Query("upcoming", description="upcoming (soonest first) | past | cancelled (newest first)"),
    cursor: Optional[str] = Query(None, description="nextCursor from the previous page"),
    limit: int = Query(20, description="1-50"),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """My Appointments: the signed-in customer's bookings."""
    return await BookingsService(db).list_mine(current_user, filter, cursor, limit)
