"""Creating and reading bookings (BK-22, BK-23)."""
import logging
import uuid as uuid_pkg
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.modules.auth.models import User
from app.modules.booking import availability
from app.modules.booking.models import Booking, BookingLine, BookingStatus
from app.modules.booking.pricing import DepositPolicy, Line, PricingError, quote_booking
from app.modules.booking.schemas import BookingLineResponse, BookingResponse, CreateBookingRequest, Money
from app.modules.booking.service import AvailabilityService, _aware
from app.modules.booking.states import record_created, transition
from app.modules.business.models import BusinessPolicies, Staff

logger = logging.getLogger(__name__)

ANY_STAFF_NAME = "Any available"


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


async def expire_holds(db: AsyncSession, now: datetime, business_id: Optional[uuid_pkg.UUID] = None) -> int:
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

        try:
            await self.db.commit()
        except IntegrityError as e:
            # The exclusion constraint caught a race the lock didn't (it shouldn't, but it's the backstop).
            await self.db.rollback()
            logger.warning(f"Booking insert rejected by the database: {e.orig}")
            raise slot_unavailable()

        return await self.get_response(booking.id)

    async def get_response(self, booking_id: uuid_pkg.UUID) -> BookingResponse:
        booking = (
            await self.db.execute(
                select(Booking).where(Booking.id == booking_id).options(selectinload(Booking.lines))
            )
        ).scalar_one()
        return await self.to_response(booking)

    async def to_response(self, booking: Booking) -> BookingResponse:
        from app.modules.business.models import Business

        business = await self.db.get(Business, booking.business_id)
        staff = await self.db.get(Staff, booking.staff_id) if booking.staff_id else None
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
            startsAt=_aware(booking.starts_at).astimezone(ZoneInfo(business.timezone if business else "UTC")),
            durationMinutes=booking.duration_minutes,
            totalAmount=Money(amount=booking.total_amount, currency=booking.currency),
            depositAmount=Money(amount=booking.deposit_amount, currency=booking.currency) if booking.deposit_amount else None,
            status=booking.status,
            source=booking.source,
            paymentStatus=booking.payment_status,
            expiresAt=_aware(booking.expires_at) if booking.expires_at else None,
            cancelReason=booking.cancel_reason,
            notes=booking.notes,
            createdAt=_aware(booking.created_at),
        )
