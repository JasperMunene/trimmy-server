"""The business side of bookings (BK-63): calendar list, walk-ins, booking on a
customer's behalf, and status changes (check-in, complete, no-show, cancel)."""
import uuid as uuid_pkg
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.auth.models import User
from app.modules.booking import availability
from app.modules.booking.bookings import BookingsService, _lock_business_day, deposit_paid, expire_holds, record_refund
from app.modules.booking.models import (
    ACTIVE_STATUSES,
    Booking,
    BookingEvent,
    BookingLine,
    BookingSource,
    BookingStatus,
    RefundStatus,
)
from app.modules.booking.pricing import Line, PricingError, quote_booking
from app.modules.booking.schemas import (
    BookingResponse,
    BusinessBookingsResponse,
    OwnerBookingRequest,
    ScheduledBookingRequest,
    UpdateBookingStatusRequest,
)
from app.modules.booking.service import AvailabilityService, _aware
from app.modules.booking.states import InvalidTransition, record_created, transition
from app.modules.business.models import Business, Staff

OWNER, FRONT_DESK, STAFF = "owner", "front_desk", "staff"
MAX_RANGE_DAYS = 62
WALK_IN_NAME = "Walk-in customer"


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


class BusinessBookingsService:
    def __init__(self, db: AsyncSession, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.now(timezone.utc)
        self.bookings = BookingsService(db, self.now)

    # ─── who is asking ───────────────────────────────────────

    async def _role(self, user: User, business_id: uuid_pkg.UUID) -> tuple[Optional[str], Optional[Staff]]:
        """(role, their Staff row) at this business: owner, front_desk, staff, or (None, None)."""
        business = await self.db.get(Business, business_id)
        if business is None:
            return None, None
        staff = (
            await self.db.execute(select(Staff).where(Staff.business_id == business_id, Staff.user_id == user.id))
        ).scalars().first()
        if business.owner_id == user.id:
            return OWNER, staff
        if staff is None:
            return None, None
        return (FRONT_DESK if staff.role == "front_desk" else STAFF), staff

    async def _require_manager(self, user: User, business_id: uuid_pkg.UUID) -> None:
        role, _ = await self._role(user, business_id)
        if role is None:
            raise _error(404, "not_found", "No business found for this id")
        if role not in (OWNER, FRONT_DESK):
            raise _error(403, "forbidden", "Only the owner or front desk can do this")

    # ─── calendar ────────────────────────────────────────────

    async def list(
        self,
        user: User,
        business_id: str,
        day: Optional[date],
        start: Optional[date],
        end: Optional[date],
        status: Optional[str],
        staff_id: Optional[uuid_pkg.UUID],
        unassigned: bool,
        refund: Optional[str] = None,
    ) -> BusinessBookingsResponse:
        try:
            bid = uuid_pkg.UUID(str(business_id))
        except ValueError:
            raise _error(404, "not_found", "No business found for this id")
        role, me = await self._role(user, bid)
        if role is None:
            raise _error(404, "not_found", "No business found for this id")

        if refund is not None and refund not in (RefundStatus.DUE, RefundStatus.REFUNDED):
            raise _error(400, "validation_error", "refund must be due or refunded")
        if day:
            start = end = day
        # Refunds owed are listed whatever their date; everything else needs a range.
        if (not start or not end) and refund is None:
            raise _error(400, "validation_error", "Pass date, or from and to")
        if start and end and (end < start or (end - start).days >= MAX_RANGE_DAYS):
            raise _error(400, "validation_error", f"from..to must be 1 to {MAX_RANGE_DAYS} days")

        business = await self.db.get(Business, bid)
        tz = ZoneInfo(business.timezone)

        if await expire_holds(self.db, self.now, business_id=bid):
            await self.db.commit()
        query = (
            select(Booking)
            .where(Booking.business_id == bid)
            .options(selectinload(Booking.lines))
            .order_by(Booking.starts_at, Booking.id)
        )
        if start and end:
            lo = datetime.combine(start, time(0, 0), tz).astimezone(timezone.utc)
            hi = datetime.combine(end + timedelta(days=1), time(0, 0), tz).astimezone(timezone.utc)
            query = query.where(Booking.starts_at < hi, Booking.ends_at > lo)
        if refund is not None:
            query = query.where(Booking.refund_status == refund)
        if role == STAFF:
            query = query.where(Booking.staff_id == me.id)  # staff see their own chair only
        elif staff_id:
            query = query.where(Booking.staff_id == staff_id)
        if unassigned:
            query = query.where(Booking.staff_id.is_(None))
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            if not set(statuses) <= {s.value for s in BookingStatus}:
                raise _error(400, "validation_error", f"Unknown status in {status!r}")
            query = query.where(Booking.status.in_(statuses))
        rows = (await self.db.execute(query)).scalars().all()
        return BusinessBookingsResponse(bookings=await self.bookings.to_responses(rows))

    # ─── walk-ins and booking on someone's behalf ────────────

    async def create_walk_in(self, user: User, data: OwnerBookingRequest) -> BookingResponse:
        """The customer is here now: stamped with the current time, starts in_progress."""
        return await self._create(user, data, starts_at=None)

    async def create_scheduled(self, user: User, data: ScheduledBookingRequest) -> BookingResponse:
        return await self._create(user, data, starts_at=(data.date, data.time))

    async def _create(self, user: User, data: OwnerBookingRequest, starts_at: Optional[tuple]) -> BookingResponse:
        await self._require_manager(user, data.business_id)
        requested = {line.service_id: line.quantity for line in data.services}
        if len(requested) != len(data.services):
            raise _error(400, "validation_error", "Each service may appear once; use quantity for more")

        slots = AvailabilityService(self.db, self.now)
        ctx = await slots.context(str(data.business_id), list(requested), data.staff_id, for_existing_booking=True)
        try:
            quote = quote_booking(
                [Line(sid, ctx.services[sid].price_amount, ctx.services[sid].duration_minutes, qty, ctx.services[sid].price_currency)
                 for sid, qty in requested.items()],
                None,  # business-recorded bookings take no deposit
            )
        except PricingError as e:
            raise _error(400, "validation_error", str(e))

        tz = ZoneInfo(ctx.settings.timezone)
        if starts_at is None:
            start = self.now.astimezone(tz).replace(second=0, microsecond=0)
        else:
            (day, hhmm) = starts_at
            start = datetime.combine(day, time(*map(int, hhmm.split(":"))), tz)
            if start < self.now:
                raise _error(400, "validation_error", "time is in the past")
            window = availability.opening_window(
                day, ctx.business.working_hours.weekly_hours if ctx.business.working_hours else None, tz
            )
            if window is None or start < window[0] or start + timedelta(minutes=quote.duration_minutes) > window[1]:
                raise _error(400, "validation_error", "time is not within the business's working hours for that date")
        end = start + timedelta(minutes=quote.duration_minutes)

        await _lock_business_day(self.db, ctx.business.id, start.date())
        await expire_holds(self.db, self.now, ctx.business.id)
        await self.db.flush()
        others = await slots.existing_bookings(ctx, start - timedelta(days=1), end + timedelta(days=1))
        if not availability.booking_fits(
            start=start, end=end, staff_id=data.staff_id, staff=ctx.staff, bookings=others,
            service_ids=ctx.service_ids, settings=ctx.settings, now=self.now,
        ):
            await self.db.rollback()
            raise _error(409, "slot_unavailable", "This time is no longer available")

        from app.modules.customers.service import clean_phone, remember_customer

        phone = clean_phone(data.customer_phone)
        booking = Booking(
            id=uuid_pkg.uuid4(),
            business_id=ctx.business.id,
            staff_id=data.staff_id,
            source=BookingSource.WALK_IN,  # business-recorded, per fulfillment.json
            starts_at=start.astimezone(timezone.utc),
            ends_at=end.astimezone(timezone.utc),
            local_date=start.date(),
            local_time=start.strftime("%H:%M"),
            duration_minutes=quote.duration_minutes,
            total_amount=quote.total,
            currency=quote.currency,
            customer_name=(data.customer_name or "").strip() or WALK_IN_NAME,
            customer_phone=phone,
            customer_email=data.customer_email,
            notes=data.notes,
        )
        for position, (sid, qty) in enumerate(requested.items()):
            service = ctx.services[sid]
            booking.lines.append(BookingLine(
                service_id=sid, position=position, name=service.name,
                duration_minutes=service.duration_minutes, price_amount=service.price_amount, quantity=qty,
            ))
        await remember_customer(self.db, ctx.business.id, booking.customer_name, phone, booking.customer_email)
        record_created(
            booking, BookingStatus.IN_PROGRESS if starts_at is None else BookingStatus.CONFIRMED,
            actor_id=user.id, reason="walk_in" if starts_at is None else "scheduled_by_business",
        )
        self.db.add(booking)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise _error(409, "slot_unavailable", "This time is no longer available")
        if booking.status == BookingStatus.CONFIRMED and booking.customer_phone:
            from app.modules.booking.tasks import notify_confirmed
            await notify_confirmed(booking.id)  # on-behalf bookings; walk-ins are already here
        return await self.bookings.get_response(booking.id)

    # ─── status changes ──────────────────────────────────────

    async def update_status(self, user: User, booking_id: str, data: UpdateBookingStatusRequest) -> BookingResponse:
        not_found = _error(404, "not_found", "bookingId does not exist")
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            raise not_found
        booking = (
            await self.db.execute(select(Booking).where(Booking.id == bid).options(selectinload(Booking.events)))
        ).scalar_one_or_none()
        if booking is None:
            raise not_found
        role, me = await self._role(user, booking.business_id)
        if role is None:
            raise not_found
        # Staff may move their own bookings along (check in, complete); nothing else.
        if role == STAFF and not (booking.staff_id == me.id and data.status in (BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED)):
            raise _error(403, "forbidden", "Only the owner or front desk can do this")

        if data.status == BookingStatus.NO_SHOW and self.now < _aware(booking.starts_at):
            raise _error(400, "invalid_status_transition", "A booking can only be marked no-show after it was due to start")
        reason = data.reason or ("business" if data.status == BookingStatus.CANCELLED else None)
        try:
            transition(booking, data.status, actor_id=user.id, reason=reason)
        except InvalidTransition as e:
            raise _error(400, "invalid_status_transition", f"Cannot move from {e.from_status} to {e.to_status}")
        if data.status == BookingStatus.CANCELLED:
            # The business called it off: the whole paid deposit goes back (D10).
            record_refund(booking, fee=0, refund=deposit_paid(booking))
        await self.db.commit()
        return await self.bookings.get_response(booking.id)

    # ─── refunds (BK-60, D10) ────────────────────────────────

    async def mark_refunded(self, user: User, booking_id: str, reference: Optional[str]) -> BookingResponse:
        """The business has sent the money back by hand; record it."""
        not_found = _error(404, "not_found", "bookingId does not exist")
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            raise not_found
        booking = await self.db.get(Booking, bid)
        if booking is None:
            raise not_found
        role, _ = await self._role(user, booking.business_id)
        if role is None:
            raise not_found
        if role not in (OWNER, FRONT_DESK):
            raise _error(403, "forbidden", "Only the owner or front desk can do this")
        if booking.refund_status != RefundStatus.DUE:
            raise _error(409, "no_refund_due", "There's no refund owed on this booking")

        booking.refund_status = RefundStatus.REFUNDED
        booking.refunded_at = self.now
        booking.refund_reference = (reference or "").strip() or None
        self.db.add(BookingEvent(
            booking_id=booking.id, from_status=booking.status, to_status=booking.status, actor_id=user.id,
            reason=f"refunded {booking.refund_reference or ''}".strip()[:100],
        ))
        await self.db.commit()
        return await self.bookings.get_response(booking.id)
