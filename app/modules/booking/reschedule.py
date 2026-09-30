"""Moving a booking to another time (BK-61, story C3)."""
from datetime import datetime, time, timedelta, timezone
from typing import Optional
import uuid as uuid_pkg
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.auth.models import User
from app.modules.booking import availability
from app.modules.booking.bookings import BookingsService, _lock_business_day, expire_holds
from app.modules.booking.models import Booking, BookingEvent, BookingStatus
from app.modules.booking.schemas import BookingResponse, RescheduleRequest
from app.modules.booking.service import AvailabilityService, _aware
from app.modules.business.models import BusinessPolicies

MOVABLE = (BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


class RescheduleService:
    def __init__(self, db: AsyncSession, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.now(timezone.utc)

    async def reschedule(self, user: User, booking_id: str, data: RescheduleRequest) -> BookingResponse:
        """The customer (their own booking, on the slot grid, inside the free window) or the
        owner / front desk (any upcoming booking, anywhere inside working hours)."""
        from app.modules.booking.business_bookings import FRONT_DESK, OWNER, BusinessBookingsService

        not_found = _error(404, "not_found", "No booking found for this id")
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            raise not_found
        booking = (
            await self.db.execute(
                select(Booking).where(Booking.id == bid).options(selectinload(Booking.lines), selectinload(Booking.events))
            )
        ).scalar_one_or_none()
        if booking is None:
            raise not_found
        role, _ = await BusinessBookingsService(self.db, self.now)._role(user, booking.business_id)
        as_business = role in (OWNER, FRONT_DESK)
        if not as_business and booking.customer_id != user.id:
            raise not_found

        if booking.status not in MOVABLE:
            raise _error(409, "cannot_reschedule", f"A {booking.status} booking can't be rescheduled")
        old_start = _aware(booking.starts_at)
        if self.now >= old_start:
            raise _error(409, "cannot_reschedule", "This appointment has already started")
        if not as_business:
            policies = (
                await self.db.execute(select(BusinessPolicies).where(BusinessPolicies.business_id == booking.business_id))
            ).scalar_one_or_none()
            free_until = old_start - timedelta(hours=policies.cancellation_free_hours if policies else 24)
            if self.now > free_until:
                raise _error(409, "reschedule_window_passed",
                             "It's too late to reschedule in the app; please contact the business")

        staff_id = data.staff_id if data.staff_id is not None else booking.staff_id
        service_ids = [line.service_id for line in booking.lines if line.service_id]
        slots = AvailabilityService(self.db, self.now)
        ctx = await slots.context(str(booking.business_id), service_ids, staff_id, for_existing_booking=True)
        tz = ZoneInfo(ctx.settings.timezone)
        new_start = datetime.combine(data.date, time(*map(int, data.time.split(":"))), tz)
        new_end = new_start + timedelta(minutes=booking.duration_minutes)
        if new_start == old_start and staff_id == booking.staff_id:
            return await BookingsService(self.db, self.now).get_response(booking.id)

        await _lock_business_day(self.db, booking.business_id, data.date)
        await expire_holds(self.db, self.now, booking.business_id)
        await self.db.flush()
        others = await slots.existing_bookings(ctx, new_start - timedelta(days=1), new_end + timedelta(days=1), exclude_id=booking.id)
        weekly = ctx.business.working_hours.weekly_hours if ctx.business.working_hours else None
        if as_business:
            window = availability.opening_window(data.date, weekly, tz)
            if new_start < self.now:
                raise _error(400, "validation_error", "time is in the past")
            if window is None or new_start < window[0] or new_end > window[1]:
                raise _error(400, "validation_error", "time is not within the business's working hours for that date")
            fits = availability.booking_fits(
                start=new_start, end=new_end, staff_id=staff_id, staff=ctx.staff, bookings=others,
                service_ids=set(service_ids), settings=ctx.settings, now=self.now,
            )
        else:
            fits = availability.slot_is_free(
                starts_at=new_start, weekly_hours=weekly, staff=ctx.staff, bookings=others,
                service_ids=set(service_ids), duration_minutes=booking.duration_minutes,
                settings=ctx.settings, now=self.now, staff_id=staff_id,
            )
        if not fits:
            await self.db.rollback()
            raise _error(409, "slot_unavailable", "The selected time is no longer available")

        old_label = f"{booking.local_date.isoformat()} {booking.local_time}"
        booking.starts_at = new_start.astimezone(timezone.utc)
        booking.ends_at = new_end.astimezone(timezone.utc)
        booking.local_date, booking.local_time = data.date, data.time
        booking.staff_id = staff_id
        booking.confirmation_sent_at = None  # the new time gets its own SMS...
        booking.reminder_24h_sent_at = booking.reminder_2h_sent_at = None  # ...and its own reminders
        booking.events.append(BookingEvent(
            from_status=booking.status, to_status=booking.status, actor_id=user.id,
            reason=f"rescheduled from {old_label}"[:100],
        ))
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise _error(409, "slot_unavailable", "The selected time is no longer available")

        if booking.status == BookingStatus.CONFIRMED:
            from app.modules.booking.tasks import notify_confirmed
            await notify_confirmed(booking.id)
        return await BookingsService(self.db, self.now).get_response(booking.id)
