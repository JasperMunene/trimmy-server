import uuid as uuid_pkg
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_verified_user
from app.modules.auth.models import User
from app.modules.booking.bookings import BookingsService
from app.modules.booking.business_bookings import BusinessBookingsService
from app.modules.booking.schemas import (
    AssignStaffRequest,
    BookingResponse,
    BusinessBookingsResponse,
    CancelBookingRequest,
    CancelPreviewResponse,
    MarkRefundedRequest,
    OwnerBookingRequest,
    ScheduledBookingRequest,
    UpdateBookingStatusRequest,
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


@availability_router.get("/{business_id}/bookings", response_model=BusinessBookingsResponse)
async def list_business_bookings(
    business_id: str,
    day: Optional[date] = Query(None, alias="date", description="One day (Today view)"),
    start: Optional[date] = Query(None, alias="from", description="Range start (Calendar, Earnings)"),
    end: Optional[date] = Query(None, alias="to", description="Range end, inclusive; at most 62 days"),
    status_filter: Optional[str] = Query(None, alias="status", description="Comma-separated, e.g. confirmed,in_progress"),
    staff_id: Optional[uuid_pkg.UUID] = Query(None, alias="staffId"),
    unassigned: bool = Query(False, description="Only bookings with no staff assigned"),
    refund: Optional[str] = Query(None, description="due | refunded; date range optional with this"),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """The business's bookings, soonest first. Owner and front desk see all; staff see their own."""
    return await BusinessBookingsService(db).list(
        current_user, business_id, day, start, end, status_filter, staff_id, unassigned, refund
    )


@bookings_router.post("/walk-in", status_code=status.HTTP_201_CREATED, response_model=BookingResponse)
async def create_walk_in_booking(
    data: OwnerBookingRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner or front desk: the customer is here now. Starts in_progress; no deposit."""
    return await BusinessBookingsService(db).create_walk_in(current_user, data)


@bookings_router.post("/scheduled", status_code=status.HTTP_201_CREATED, response_model=BookingResponse)
async def create_scheduled_booking(
    data: ScheduledBookingRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner or front desk books on a customer's behalf. Starts confirmed; no deposit."""
    return await BusinessBookingsService(db).create_scheduled(current_user, data)


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


@bookings_router.get("/{booking_id}/cancel-preview", response_model=CancelPreviewResponse)
async def cancel_preview(
    booking_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Customer: what cancelling now would cost (fee kept, refund owed) before confirming."""
    return await BookingsService(db).cancel_preview(current_user, booking_id)


@bookings_router.post("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(
    booking_id: str,
    data: CancelBookingRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Customer cancels within policy. Any refund is owed by the business, which pays it by hand."""
    return await BookingsService(db).cancel(current_user, booking_id, data.reason)


@bookings_router.post("/{booking_id}/refund", response_model=BookingResponse)
async def mark_refunded(
    booking_id: str,
    data: MarkRefundedRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner or front desk: record that a refund owed on this booking has been paid back."""
    return await BusinessBookingsService(db).mark_refunded(current_user, booking_id, data.reference)


@bookings_router.patch("/{booking_id}/status", response_model=BookingResponse)
async def update_booking_status(
    booking_id: str,
    data: UpdateBookingStatusRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Check in (in_progress), complete, mark no-show (after the start time) or cancel.
    Owner and front desk for any booking; staff can check in and complete their own."""
    return await BusinessBookingsService(db).update_status(current_user, booking_id, data)


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
