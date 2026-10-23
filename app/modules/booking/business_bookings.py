"""The business side of bookings (BK-63): calendar list, walk-ins, booking on a
customer's behalf, and status changes (check-in, complete, no-show, cancel)."""
import uuid as uuid_pkg
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_, select, update
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
    Visit,
)
from app.modules.booking.pricing import Line, PricingError, quote_booking
from app.modules.booking.schemas import (
    AddServiceRequest,
    BookingResponse,
    BusinessBookingsResponse,
    OwnerBookingRequest,
    ScheduledBookingRequest,
    UpdateBookingStatusRequest,
    VisitResponse,
)
from app.modules.booking.service import AvailabilityService, _aware
from app.modules.booking.states import InvalidTransition, record_created, transition
from app.modules.business.models import Business, Staff

OWNER, FRONT_DESK, STAFF = "owner", "front_desk", "staff"
MAX_RANGE_DAYS = 62
CUSTOMER_HISTORY_LIMIT = 200
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
        # Front desk access (desk-only, or staff who also run the desk) manages the business's bookings.
        return (FRONT_DESK if staff.role == "front_desk" or staff.front_desk_access else STAFF), staff

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
        customer_id: Optional[uuid_pkg.UUID] = None,
        phone: Optional[str] = None,
        limit: Optional[int] = None,
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
        # One customer's history (Customer Detail): their bookings by phone, or by email for a
        # customer saved without one. Newest first, any date.
        history = customer_id is not None or bool(phone)
        match = None
        if history:
            from app.modules.customers.models import BusinessCustomer
            from app.modules.customers.service import clean_phone

            if customer_id is not None:
                saved = await self.db.get(BusinessCustomer, customer_id)
                if saved is None or saved.business_id != bid:
                    raise _error(404, "not_found", "No customer found for this id")
                if saved.phone:
                    match = Booking.customer_phone == saved.phone
                elif saved.email:
                    match = Booking.customer_email == saved.email
            else:
                match = Booking.customer_phone == clean_phone(phone)
        # Refunds owed and a customer's history are listed whatever their date; everything else needs a range.
        if (not start or not end) and refund is None and not history:
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
        if history:
            if match is None:
                return BusinessBookingsResponse(bookings=[])
            query = query.where(match).order_by(None).order_by(Booking.starts_at.desc(), Booking.id)
            query = query.limit(min(limit or CUSTOMER_HISTORY_LIMIT, CUSTOMER_HISTORY_LIMIT))
        rows = (await self.db.execute(query)).scalars().all()
        return BusinessBookingsResponse(bookings=await self.bookings.to_responses(rows))

    # ─── walk-ins and booking on someone's behalf ────────────

    async def create_walk_in(self, user: User, data: OwnerBookingRequest) -> BookingResponse:
        """The customer is here now: stamped with the current time, starts in_progress."""
        return await self._create(user, data, starts_at=None)

    async def create_scheduled(self, user: User, data: ScheduledBookingRequest) -> BookingResponse:
        return await self._create(user, data, starts_at=(data.date, data.time))

    async def _create(
        self, user: User, data: OwnerBookingRequest, starts_at: Optional[tuple], *, visit_source: Optional[Booking] = None,
    ) -> BookingResponse:
        """starts_at None = now (a walk-in, in progress), else (date, "HH:MM") confirmed. With
        visit_source, the new booking joins that booking's visit (made on first use): same customer,
        no confirmation message (they're already here)."""
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
        visit_id = None
        if visit_source is not None:
            # The source row, locked: two services added at once join one visit.
            visit_id = (
                await self.db.execute(select(Booking.visit_id).where(Booking.id == visit_source.id).with_for_update())
            ).scalar_one()
            if visit_id is None:
                visit = Visit(id=uuid_pkg.uuid4(), business_id=ctx.business.id, created_by=user.id)
                self.db.add(visit)
                await self.db.flush()
                visit_id = visit.id
                await self.db.execute(update(Booking).where(Booking.id == visit_source.id).values(visit_id=visit_id))
        await expire_holds(self.db, self.now, ctx.business.id)
        await self.db.flush()
        others = await slots.existing_bookings(ctx, start - timedelta(days=1), end + timedelta(days=1))
        if not availability.booking_fits(
            start=start, end=end, staff_id=data.staff_id, staff=ctx.staff, bookings=others,
            service_ids=ctx.service_ids, settings=ctx.settings, now=self.now,
        ):
            await self.db.rollback()
            raise _error(409, "slot_unavailable", "This time is no longer available")

        from app.modules.booking.customer_match import match_account
        from app.modules.customers.service import clean_phone, remember_customer

        phone = clean_phone(data.customer_phone)
        booking = Booking(
            id=uuid_pkg.uuid4(),
            business_id=ctx.business.id,
            # A Trimmy account with this proven phone / email: the booking shows in their app and
            # they get its notifications (customer_match.py).
            customer_id=(
                visit_source.customer_id if visit_source is not None
                else await match_account(self.db, phone, data.customer_email)
            ),
            staff_id=data.staff_id,
            visit_id=visit_id,
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
        if visit_source is None:
            await remember_customer(self.db, ctx.business.id, booking.customer_name, phone, booking.customer_email)
        record_created(
            booking, BookingStatus.IN_PROGRESS if starts_at is None else BookingStatus.CONFIRMED,
            actor_id=user.id,
            reason="visit_add" if visit_source is not None else "walk_in" if starts_at is None else "scheduled_by_business",
        )
        self.db.add(booking)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise _error(409, "slot_unavailable", "This time is no longer available")
        from app.modules.booking import tasks as booking_tasks
        if visit_source is None and booking.status == BookingStatus.CONFIRMED and (
            booking.customer_phone or booking.customer_id or booking.customer_email
        ):
            # On-behalf bookings: SMS if there's a phone; in the app and by email as their settings
            # allow when it's their account; email for a guest. Walk-ins are already here.
            await booking_tasks.notify_confirmed(booking.id)
        if booking.staff_id:
            await booking_tasks.push_update(booking.id, "assigned", actor_id=user.id)
        return await self.bookings.get_response(booking.id)

    # ─── visits: another service, maybe by someone else ──────

    async def add_service(self, user: User, booking_id: str, data: AddServiceRequest) -> VisitResponse:
        """A customer having one thing done wants another (hair, now nails): a booking for it,
        with whoever does it, in the same visit so it's checked out and tipped together."""
        source = await self._booking_or_404(booking_id)
        business_id = source.business_id  # read now: a failed insert rolls back and expires `source`
        await self._require_manager(user, business_id)
        if source.status not in (BookingStatus.CONFIRMED, BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED):
            raise _error(409, "invalid_source_status", "Services can only be added to a booked, started or finished visit")

        request = OwnerBookingRequest(
            businessId=source.business_id, customerName=source.customer_name, customerPhone=source.customer_phone,
            customerEmail=source.customer_email, services=data.services, staffId=data.staff_id, notes=data.notes,
        )
        tz = ZoneInfo((await self.db.get(Business, business_id)).timezone)
        if data.start == "now":
            starts_at = None
        else:
            if data.start == "at":
                if not data.time:
                    raise _error(400, "validation_error", "time is required when start is at")
                local = datetime.combine(self.now.astimezone(tz).date(), time(*map(int, data.time.split(":"))), tz)
            else:
                # Right after the booking it's added to (or now, if that's already over).
                local = max(_aware(source.ends_at), self.now).astimezone(tz)
            # Whole minutes, never in the past.
            if local.second or local.microsecond:
                local = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
            starts_at = (local.date(), local.strftime("%H:%M"))
        try:
            created = await self._create(user, request, starts_at, visit_source=source)
        except HTTPException as e:
            if e.status_code == 409 and isinstance(e.detail, dict) and e.detail.get("error") == "slot_unavailable":
                next_free = await self._next_free(request, business_id, tz)
                raise HTTPException(status_code=409, detail={
                    **e.detail,
                    "message": "They're busy then" + (f"; free from {next_free}" if next_free else " for the rest of today"),
                    "nextFreeAt": next_free,
                })
            raise
        from app.modules.booking.visits import VisitsService
        return await VisitsService(self.db, self.now).get(user, str(created.visit_id))

    async def _next_free(self, data: OwnerBookingRequest, business_id: uuid_pkg.UUID, tz: ZoneInfo) -> Optional[str]:
        """The first time today (HH:MM, every 5 minutes) this person can fit these services."""
        requested = {line.service_id: line.quantity for line in data.services}
        slots = AvailabilityService(self.db, self.now)
        ctx = await slots.context(str(business_id), list(requested), data.staff_id, for_existing_booking=True)
        minutes = sum(ctx.services[sid].duration_minutes * qty for sid, qty in requested.items())
        today = self.now.astimezone(tz).date()
        window = availability.opening_window(
            today, ctx.business.working_hours.weekly_hours if ctx.business.working_hours else None, tz
        )
        if window is None:
            return None
        start = max(self.now.astimezone(tz), window[0]).replace(second=0, microsecond=0)
        start += timedelta(minutes=(5 - start.minute % 5) % 5)
        others = await slots.existing_bookings(ctx, start - timedelta(days=1), window[1] + timedelta(days=1))
        while start + timedelta(minutes=minutes) <= window[1]:
            end = start + timedelta(minutes=minutes)
            if availability.booking_fits(
                start=start, end=end, staff_id=data.staff_id, staff=ctx.staff, bookings=others,
                service_ids=ctx.service_ids, settings=ctx.settings, now=self.now,
            ):
                return start.strftime("%H:%M")
            start += timedelta(minutes=5)
        return None

    async def _booking_or_404(self, booking_id: str) -> Booking:
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            raise _error(404, "not_found", "bookingId does not exist")
        booking = await self.db.get(Booking, bid)
        if booking is None:
            raise _error(404, "not_found", "bookingId does not exist")
        return booking

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
        elif data.status == BookingStatus.NO_SHOW:
            await self._apply_no_show_fee(booking)
        await self.db.commit()
        from app.modules.booking.tasks import push_update
        if data.status == BookingStatus.CANCELLED:
            await push_update(booking.id, "cancelled_by_business")
            await push_update(booking.id, "cancelled_for_staff", actor_id=user.id)
        elif data.status == BookingStatus.NO_SHOW:
            await push_update(booking.id, "no_show")
        elif data.status == BookingStatus.COMPLETED and booking.payment_status == "paid":
            # Unpaid: the receipt goes out when it's charged (payments/service.py mark_charged).
            await push_update(booking.id, "completed")
        return await self.bookings.get_response(booking.id)

    async def _apply_no_show_fee(self, booking: Booking) -> None:
        """Keep the policy's no-show fee from the paid deposit (BK-64); refund what's left."""
        from app.modules.booking.cancellation import no_show_outcome
        from app.modules.business.models import BusinessPolicies

        policies = (
            await self.db.execute(select(BusinessPolicies).where(BusinessPolicies.business_id == booking.business_id))
        ).scalar_one_or_none()
        percent = policies.no_show_fee_percent if policies else 100
        result = no_show_outcome(total=booking.total_amount, deposit_paid=deposit_paid(booking), fee_percent=percent)
        booking.no_show_fee_percent = percent
        booking.no_show_fee = result.kept
        booking.no_show_fee_uncollected = result.uncollected
        if result.refund > 0:
            booking.refund_amount, booking.refund_status = result.refund, RefundStatus.DUE

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
        from app.modules.booking.tasks import push_update
        await push_update(booking.id, "refunded")
        return await self.bookings.get_response(booking.id)
