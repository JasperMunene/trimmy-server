"""Creating and reading bookings (BK-22, BK-23)."""
import base64
import binascii
import logging
import uuid as uuid_pkg
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.modules.auth.models import User
from app.modules.booking import availability, cancellation
from app.modules.booking.models import (
    ACTIVE_STATUSES,
    Booking,
    BookingEvent,
    BookingLine,
    BookingStatus,
    PaymentStatus,
    RefundStatus,
)
from app.modules.booking.pricing import DepositPolicy, Line, PricingError, quote_booking
from app.modules.booking.schemas import (
    BookingLineResponse,
    CancelPreviewResponse,
    NoShowFeeResponse,
    RefundResponse,
    DepositPaymentResponse,
    BookingResponse,
    CreateBookingRequest,
    ListBookingsResponse,
    Money,
)
from app.modules.booking.service import AvailabilityService, _aware
from app.modules.booking.states import record_created, transition
from app.modules.business.models import Business, BusinessPolicies, Staff
from app.modules.payments.models import Payment, PaymentKind

logger = logging.getLogger(__name__)

ANY_STAFF_NAME = "Any available"
FILTERS = ("upcoming", "past", "cancelled")
PAGE_SIZE_MAX = 50


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def slot_unavailable() -> HTTPException:
    return _error(409, "slot_unavailable", "The selected time is no longer available")


def deposit_policy(p: Optional[BusinessPolicies]) -> Optional[DepositPolicy]:
    if p is None:
        return None
    return DepositPolicy(
        required=p.deposit_required,
        type=p.deposit_type,
        amount=p.deposit_amount,
        percent=p.deposit_percent,
        applies_to=p.deposit_applies_to,
        service_ids=frozenset(p.deposit_service_ids or []),
    )


async def expire_holds(
    db: AsyncSession,
    now: datetime,
    business_id: Optional[uuid_pkg.UUID] = None,
    customer_id: Optional[uuid_pkg.UUID] = None,
) -> int:
    """Cancel pending_payment bookings whose hold has run out (D6). The caller commits.

    Used by the periodic sweep (BK-24) and, for one business, right before a new
    booking is checked, so an expired hold can't trip the exclusion constraint.
    """
    query = select(Booking).where(
        Booking.status == BookingStatus.PENDING_PAYMENT,
        Booking.expires_at.is_not(None),
        Booking.expires_at <= now,
    ).options(selectinload(Booking.events))
    if business_id is not None:
        query = query.where(Booking.business_id == business_id)
    if customer_id is not None:
        query = query.where(Booking.customer_id == customer_id)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    expired = (await db.execute(query)).scalars().all()
    for booking in expired:
        transition(booking, BookingStatus.CANCELLED, reason="payment_expired")
    return len(expired)


async def _lock_business_day(db: AsyncSession, business_id: uuid_pkg.UUID, day) -> None:
    """Serialise bookings for one business and day until this transaction ends (D9).

    The exclusion constraint only covers assigned bookings; this also covers
    unassigned "any available" ones. SQLite (dev/tests) allows one writer anyway.
    """
    if db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"booking:{business_id}:{day}"}
        )


def _encode_cursor(booking: Booking) -> str:
    raw = f"{_aware(booking.starts_at).isoformat()}|{booking.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid_pkg.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        when, booking_id = raw.split("|")
        return datetime.fromisoformat(when).astimezone(timezone.utc), uuid_pkg.UUID(booking_id)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise _error(400, "validation_error", "Invalid cursor")


def deposit_paid(booking: Booking) -> int:
    """The deposit actually paid on this booking (0 if none was, or it's still unpaid)."""
    if booking.payment_status != PaymentStatus.DEPOSIT_PAID:
        return 0
    return booking.deposit_amount or 0


def _settling(deposit: Optional[Payment], charge: Optional[Payment]) -> tuple[Optional[str], Optional[str]]:
    """(paymentMethod, paymentReference) for the response: the checkout charge once it
    succeeded, else a paid deposit's M-Pesa receipt."""
    if charge is not None and charge.status == "succeeded":
        return charge.provider, charge.mpesa_receipt
    if deposit is not None and deposit.mpesa_receipt:
        return "mpesa", deposit.mpesa_receipt
    return None, None


def record_refund(booking: Booking, fee: int, refund: int) -> None:
    """Store a cancellation's money: the fee kept, and any refund the business now owes (D10)."""
    booking.cancellation_fee = fee or None
    if refund > 0:
        booking.refund_amount = refund
        booking.refund_status = RefundStatus.DUE


class BookingsService:
    def __init__(self, db: AsyncSession, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.now(timezone.utc)

    async def create(self, customer: User, data: CreateBookingRequest) -> BookingResponse:
        requested = {line.service_id: line.quantity for line in data.services}
        if len(requested) != len(data.services):
            raise _error(400, "validation_error", "Each service may appear once; use quantity for more")

        slots = AvailabilityService(self.db, self.now)
        ctx = await slots.context(str(data.business_id), list(requested), data.staff_id)
        business = ctx.business
        if business.owner_id == customer.id:
            raise _error(400, "own_business", "You can't book your own business as a customer")

        try:
            quote = quote_booking(
                [
                    Line(
                        service_id=sid,
                        price_amount=ctx.services[sid].price_amount,
                        duration_minutes=ctx.services[sid].duration_minutes,
                        quantity=qty,
                        currency=ctx.services[sid].price_currency,
                    )
                    for sid, qty in requested.items()
                ],
                deposit_policy(business.policies),
            )
        except PricingError as e:
            raise _error(400, "validation_error", str(e))

        if data.expected_total and (data.expected_total.amount, data.expected_total.currency) != (quote.total, quote.currency):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "price_changed",
                    "message": "Prices have changed since you started; please review the new total",
                    "totalAmount": {"amount": quote.total, "currency": quote.currency},
                },
            )

        tz = ZoneInfo(ctx.settings.timezone)
        hh, mm = map(int, data.time.split(":"))
        starts_at = datetime.combine(data.date, time(hh, mm), tz)
        ends_at = starts_at + timedelta(minutes=quote.duration_minutes)

        # Everything from here to commit is one transaction under the lock.
        await _lock_business_day(self.db, business.id, data.date)
        await expire_holds(self.db, self.now, business.id)
        await self.db.flush()
        existing = await slots.existing_bookings(ctx, starts_at - timedelta(days=1), ends_at + timedelta(days=1))
        free = availability.slot_is_free(
            starts_at=starts_at,
            weekly_hours=business.working_hours.weekly_hours if business.working_hours else None,
            staff=ctx.staff,
            bookings=existing,
            service_ids=ctx.service_ids,
            duration_minutes=quote.duration_minutes,
            settings=ctx.settings,
            now=self.now,
            staff_id=data.staff_id,
        )
        if not free:
            await self.db.rollback()
            raise slot_unavailable()

        booking = Booking(
            id=uuid_pkg.uuid4(),
            business_id=business.id,
            customer_id=customer.id,
            staff_id=data.staff_id,
            starts_at=starts_at.astimezone(timezone.utc),
            ends_at=ends_at.astimezone(timezone.utc),
            local_date=data.date,
            local_time=data.time,
            duration_minutes=quote.duration_minutes,
            total_amount=quote.total,
            currency=quote.currency,
            deposit_amount=quote.deposit,
            customer_name=" ".join(p for p in (customer.first_name, customer.last_name) if p) or customer.email or "Customer",
            customer_phone=customer.phone,
            customer_email=customer.email,
            notes=data.notes,
        )
        for position, (sid, qty) in enumerate(requested.items()):
            service = ctx.services[sid]
            booking.lines.append(
                BookingLine(
                    service_id=sid, position=position, name=service.name,
                    duration_minutes=service.duration_minutes, price_amount=service.price_amount, quantity=qty,
                )
            )
        if quote.deposit:
            record_created(booking, BookingStatus.PENDING_PAYMENT, actor_id=customer.id)
            booking.expires_at = self.now + timedelta(minutes=settings.BOOKING_HOLD_MINUTES)
        else:
            record_created(booking, BookingStatus.CONFIRMED, actor_id=customer.id)
        self.db.add(booking)
        from app.modules.customers.service import remember_customer
        await remember_customer(self.db, business.id, booking.customer_name, booking.customer_phone, booking.customer_email)

        try:
            await self.db.commit()
        except IntegrityError as e:
            # The exclusion constraint caught a race the lock didn't (it shouldn't, but it's the backstop).
            await self.db.rollback()
            logger.warning(f"Booking insert rejected by the database: {e.orig}")
            raise slot_unavailable()

        from app.modules.booking import tasks as booking_tasks
        if booking.status == BookingStatus.CONFIRMED:
            await booking_tasks.notify_confirmed(booking.id)
        if booking.staff_id:
            await booking_tasks.push_update(booking.id, "assigned")
        return await self.get_response(booking.id)

    # ─── cancelling (BK-60) ──────────────────────────────────

    async def _own_booking(self, user: User, booking_id: str) -> Booking:
        not_found = _error(404, "not_found", "No booking found for this id")
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            raise not_found
        booking = (
            await self.db.execute(select(Booking).where(Booking.id == bid).options(selectinload(Booking.events)))
        ).scalar_one_or_none()
        if booking is None or booking.customer_id != user.id:
            raise not_found
        return booking

    async def _outcome(self, booking: Booking) -> tuple[cancellation.CancellationOutcome, int]:
        policies = (
            await self.db.execute(select(BusinessPolicies).where(BusinessPolicies.business_id == booking.business_id))
        ).scalar_one_or_none()
        late_fee = policies.cancellation_late_fee_percent if policies else 50
        result = cancellation.outcome(
            status=booking.status,
            starts_at=_aware(booking.starts_at),
            total=booking.total_amount,
            deposit_paid=deposit_paid(booking),
            free_hours=policies.cancellation_free_hours if policies else 24,
            late_fee_percent=late_fee,
            now=self.now,
        )
        return result, late_fee

    async def cancel_preview(self, user: User, booking_id: str) -> CancelPreviewResponse:
        booking = await self._own_booking(user, booking_id)
        result, late_fee = await self._outcome(booking)
        cur = booking.currency
        if not result.can_cancel:
            message = result.reason_blocked
        elif result.deposit_paid == 0:
            message = "Free to cancel: no deposit was paid."
        elif result.fee == 0:
            message = f"Free cancellation: your {cur} {result.refund:,} deposit will be refunded by the business."
        else:
            message = (
                f"Late cancellation: {cur} {result.fee:,} of your deposit is kept"
                + (f" and {cur} {result.refund:,} will be refunded by the business." if result.refund else ".")
            )
        return CancelPreviewResponse(
            bookingId=booking.id,
            canCancel=result.can_cancel,
            reasonBlocked=result.reason_blocked,
            freeUntil=result.free_until,
            withinFreeWindow=result.within_free_window,
            depositPaid=Money(amount=result.deposit_paid, currency=cur),
            fee=Money(amount=result.fee, currency=cur),
            refund=Money(amount=result.refund, currency=cur),
            lateFeePercent=late_fee,
            message=message,
        )

    async def cancel(self, user: User, booking_id: str, reason: Optional[str]) -> BookingResponse:
        booking = await self._own_booking(user, booking_id)
        result, _ = await self._outcome(booking)
        if not result.can_cancel:
            raise _error(409, "cannot_cancel", result.reason_blocked)
        transition(booking, BookingStatus.CANCELLED, actor_id=user.id, reason="customer")
        record_refund(booking, result.fee, result.refund)
        if reason:
            booking.events[-1].reason = f"customer: {reason}"[:100]
        await self.db.commit()
        return await self.get_response(booking.id)

    # ─── assigning staff (BK-26) ─────────────────────────────

    async def assign_staff(self, user: User, booking_id: str, staff_id: uuid_pkg.UUID) -> BookingResponse:
        """Give an unassigned ("any available") booking to someone, or reassign it."""
        not_found = _error(404, "not_found", "No booking found for this id")
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            raise not_found
        booking = (
            await self.db.execute(select(Booking).where(Booking.id == bid).options(selectinload(Booking.lines)))
        ).scalar_one_or_none()
        if booking is None or not await self._can_view(user, booking):
            raise not_found
        if not await self._can_manage(user, booking.business_id):
            raise _error(403, "forbidden", "Only the owner or front desk can assign staff")
        if booking.status not in ACTIVE_STATUSES:
            raise _error(409, "booking_not_active", f"A {booking.status} booking can't be reassigned")
        if booking.staff_id == staff_id:
            return await self.get_response(booking.id)

        slots = AvailabilityService(self.db, self.now)
        service_ids = [line.service_id for line in booking.lines if line.service_id]
        ctx = await slots.context(str(booking.business_id), service_ids, None, for_existing_booking=True)
        if not any(s.id == staff_id for s in ctx.staff):
            raise _error(404, "not_found", "staffId does not exist for this business")

        start, end = _aware(booking.starts_at), _aware(booking.ends_at)
        await _lock_business_day(self.db, booking.business_id, booking.local_date)
        others = await slots.existing_bookings(ctx, start - timedelta(days=1), end + timedelta(days=1), exclude_id=booking.id)
        if not availability.booking_fits(
            start=start, end=end, staff_id=staff_id, staff=ctx.staff, bookings=others,
            service_ids=set(service_ids), settings=ctx.settings, now=self.now,
        ):
            await self.db.rollback()
            raise _error(409, "staff_unavailable", "That staff member can't take this booking: busy, off that day, or doesn't offer the service")

        previous = booking.staff_id
        booking.staff_id = staff_id
        self.db.add(BookingEvent(
            booking_id=booking.id, from_status=booking.status, to_status=booking.status, actor_id=user.id,
            reason="staff_reassigned" if previous else "staff_assigned",
        ))
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise _error(409, "staff_unavailable", "That staff member is already booked at this time")
        from app.modules.booking import tasks as booking_tasks
        await booking_tasks.push_update(booking.id, "assigned")
        return await self.get_response(booking.id)

    async def _can_manage(self, user: User, business_id: uuid_pkg.UUID) -> bool:
        """The owner, or someone working there as front desk."""
        business = await self.db.get(Business, business_id)
        if business and business.owner_id == user.id:
            return True
        front_desk = await self.db.execute(
            select(Staff.id).where(Staff.business_id == business_id, Staff.user_id == user.id, Staff.role == "front_desk")
        )
        return front_desk.first() is not None

    # ─── reading ─────────────────────────────────────────────

    async def get(self, user: User, booking_id: str) -> BookingResponse:
        """A booking the user took, or one at a business they own or work at; otherwise 404."""
        not_found = _error(404, "not_found", "No booking found for this id")
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            raise not_found
        booking = await self.db.get(Booking, bid)
        if booking is None or not await self._can_view(user, booking):
            raise not_found
        # Housekeeping so an expired hold never shows as still pending (the sweep, BK-24, does the rest).
        if await expire_holds(self.db, self.now, business_id=booking.business_id):
            await self.db.commit()
        return await self.get_response(bid)

    async def list_mine(self, user: User, filter: str, cursor: Optional[str], limit: int) -> ListBookingsResponse:
        if filter not in FILTERS:
            raise _error(400, "validation_error", f"filter must be one of {', '.join(FILTERS)}")
        if not 1 <= limit <= PAGE_SIZE_MAX:
            raise _error(400, "validation_error", f"limit must be between 1 and {PAGE_SIZE_MAX}")
        if await expire_holds(self.db, self.now, customer_id=user.id):
            await self.db.commit()

        query = select(Booking).where(Booking.customer_id == user.id).options(selectinload(Booking.lines))
        if filter == "upcoming":
            # Soonest first; in_progress stays "upcoming" until it ends.
            query = query.where(Booking.status.in_(ACTIVE_STATUSES), Booking.ends_at > self.now)
            ascending = True
        elif filter == "past":
            query = query.where(
                or_(
                    Booking.status.in_((BookingStatus.COMPLETED, BookingStatus.NO_SHOW)),
                    and_(Booking.status.in_(ACTIVE_STATUSES), Booking.ends_at <= self.now),
                )
            )
            ascending = False
        else:
            query = query.where(Booking.status == BookingStatus.CANCELLED)
            ascending = False

        if cursor:
            when, last_id = _decode_cursor(cursor)
            if ascending:
                query = query.where(or_(Booking.starts_at > when, and_(Booking.starts_at == when, Booking.id > last_id)))
            else:
                query = query.where(or_(Booking.starts_at < when, and_(Booking.starts_at == when, Booking.id < last_id)))
        order = (Booking.starts_at.asc(), Booking.id.asc()) if ascending else (Booking.starts_at.desc(), Booking.id.desc())
        rows = (await self.db.execute(query.order_by(*order).limit(limit + 1))).scalars().all()

        page, more = rows[:limit], len(rows) > limit
        return ListBookingsResponse(
            bookings=await self.to_responses(page),
            nextCursor=_encode_cursor(page[-1]) if more else None,
        )

    async def _can_view(self, user: User, booking: Booking) -> bool:
        if booking.customer_id == user.id:
            return True
        business = await self.db.get(Business, booking.business_id)
        if business and business.owner_id == user.id:
            return True
        works_there = await self.db.execute(
            select(Staff.id).where(Staff.business_id == booking.business_id, Staff.user_id == user.id)
        )
        return works_there.first() is not None

    # ─── responses ───────────────────────────────────────────

    async def get_response(self, booking_id: uuid_pkg.UUID) -> BookingResponse:
        booking = (
            await self.db.execute(
                select(Booking).where(Booking.id == booking_id).options(selectinload(Booking.lines))
            )
        ).scalar_one()
        return (await self.to_responses([booking]))[0]

    async def to_responses(self, bookings: list[Booking]) -> list[BookingResponse]:
        """Build responses with one query each for the businesses and staff involved."""
        business_ids = {b.business_id for b in bookings}
        staff_ids = {b.staff_id for b in bookings if b.staff_id}
        businesses = {
            b.id: b for b in (await self.db.execute(select(Business).where(Business.id.in_(business_ids)))).scalars()
        } if business_ids else {}
        staff = {
            s.id: s for s in (await self.db.execute(select(Staff).where(Staff.id.in_(staff_ids)))).scalars()
        } if staff_ids else {}
        from app.modules.booking.reviews import review_response
        from app.modules.business.models import Review

        reviews: dict = {}
        if bookings:
            for r in (await self.db.execute(select(Review).where(Review.booking_id.in_([b.id for b in bookings])))).scalars():
                reviews[r.booking_id] = review_response(r)
        latest_deposit: dict = {}
        latest_charge: dict = {}
        if bookings:
            payments = await self.db.execute(
                select(Payment).where(Payment.booking_id.in_([b.id for b in bookings])).order_by(Payment.created_at)
            )
            for p in payments.scalars():  # ordered, so the last of each kind wins
                (latest_charge if p.kind == PaymentKind.CHARGE else latest_deposit)[p.booking_id] = p
        responses = [
            self._response(
                b, businesses.get(b.business_id), staff.get(b.staff_id), latest_deposit.get(b.id), latest_charge.get(b.id)
            )
            for b in bookings
        ]
        for response in responses:
            response.review = reviews.get(response.booking_id)
        return responses

    @staticmethod
    def _response(
        booking: Booking,
        business: Optional[Business],
        staff: Optional[Staff],
        deposit: Optional[Payment] = None,
        charge: Optional[Payment] = None,
    ) -> BookingResponse:
        tz = ZoneInfo(business.timezone) if business else timezone.utc
        return BookingResponse(
            bookingId=booking.id,
            reference=booking.reference,
            businessId=booking.business_id,
            businessName=business.name if business else "",
            customerName=booking.customer_name,
            customerPhone=booking.customer_phone,
            customerEmail=booking.customer_email,
            staffId=booking.staff_id,
            staffName=staff.name if staff else ANY_STAFF_NAME,
            services=[
                BookingLineResponse(
                    serviceId=line.service_id,
                    name=line.name,
                    durationMinutes=line.duration_minutes,
                    price=Money(amount=line.price_amount, currency=booking.currency),
                    quantity=line.quantity,
                )
                for line in booking.lines
            ],
            date=booking.local_date,
            time=booking.local_time,
            startsAt=_aware(booking.starts_at).astimezone(tz),
            durationMinutes=booking.duration_minutes,
            totalAmount=Money(amount=booking.total_amount, currency=booking.currency),
            depositAmount=Money(amount=booking.deposit_amount, currency=booking.currency) if booking.deposit_amount else None,
            status=booking.status,
            source=booking.source,
            paymentStatus=booking.payment_status,
            expiresAt=_aware(booking.expires_at) if booking.expires_at else None,
            cancelReason=booking.cancel_reason,
            depositPayment=(
                DepositPaymentResponse(paymentId=deposit.id, status=deposit.status, failureReason=deposit.failure_reason)
                if deposit else None
            ),
            paymentReference=_settling(deposit, charge)[1],
            cancellationFee=Money(amount=booking.cancellation_fee, currency=booking.currency) if booking.cancellation_fee else None,
            noShowFee=(
                NoShowFeeResponse(
                    percent=booking.no_show_fee_percent,
                    fee=Money(amount=(booking.no_show_fee or 0) + (booking.no_show_fee_uncollected or 0), currency=booking.currency),
                    kept=Money(amount=booking.no_show_fee or 0, currency=booking.currency),
                    uncollected=Money(amount=booking.no_show_fee_uncollected or 0, currency=booking.currency),
                )
                if booking.no_show_fee_percent is not None else None
            ),
            refund=(
                RefundResponse(
                    amount=Money(amount=booking.refund_amount or 0, currency=booking.currency),
                    status=booking.refund_status,
                    refundedAt=_aware(booking.refunded_at) if booking.refunded_at else None,
                    reference=booking.refund_reference,
                )
                if booking.refund_status else None
            ),
            paymentMethod=_settling(deposit, charge)[0],
            balanceDue=Money(
                amount=0 if booking.payment_status == PaymentStatus.PAID else max(booking.total_amount - deposit_paid(booking), 0),
                currency=booking.currency,
            ),
            chargePayment=(
                DepositPaymentResponse(paymentId=charge.id, status=charge.status, failureReason=charge.failure_reason)
                if charge else None
            ),
            notes=booking.notes,
            createdAt=_aware(booking.created_at),
        )
