"""Applying an M-Pesa result to a payment and its booking (BK-32).

One code path for both the Daraja callback and reconciliation (BK-33), so a
result is applied the same way whichever arrives first, and exactly once.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.booking import availability
from app.modules.booking.bookings import _lock_business_day
from app.modules.booking.models import Booking, BookingEvent, BookingStatus, PaymentStatus as BookingPaymentStatus
from app.modules.booking.service import AvailabilityService, _aware
from app.modules.booking.states import REINSTATABLE_CANCEL_REASON, transition
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.payments.mpesa import PaymentResult

logger = logging.getLogger(__name__)


async def apply_result(
    db: AsyncSession, result: PaymentResult, now: datetime, raw: Optional[dict] = None
) -> Optional[Payment]:
    """Record `result` on its payment and update the booking. Commits.

    Returns the payment, or None if the CheckoutRequestID is unknown. A payment
    that's already settled is left alone (Daraja retries callbacks).
    """
    query = select(Payment).where(Payment.checkout_request_id == result.checkout_request_id)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    payment = (await db.execute(query)).scalar_one_or_none()
    if payment is None:
        logger.warning(f"M-Pesa result for unknown CheckoutRequestID {result.checkout_request_id}")
        return None
    if payment.status != PaymentStatus.PENDING or result.status == "pending":
        return payment  # duplicate, or nothing to record yet

    payment.status = result.status
    payment.failure_reason = result.failure_reason
    payment.result_code = result.result_code
    payment.result_desc = result.result_desc
    payment.mpesa_receipt = result.receipt
    payment.settled_at = now
    if raw is not None:
        payment.raw_callback = raw

    confirmed = False
    if result.status == PaymentStatus.SUCCEEDED:
        if result.amount is not None and result.amount != payment.amount:
            logger.error(f"Payment {payment.id}: M-Pesa reports KES {result.amount}, expected {payment.amount}")
        confirmed = await _deposit_paid(db, payment, now)
    booking_id = payment.booking_id
    await db.commit()
    if confirmed:
        from app.modules.booking.tasks import notify_confirmed
        await notify_confirmed(booking_id)
    return payment


async def _deposit_paid(db: AsyncSession, payment: Payment, now: datetime) -> bool:
    """Mark the deposit paid on the booking. True if that confirmed it."""
    booking = (
        await db.execute(
            select(Booking).where(Booking.id == payment.booking_id).options(
                selectinload(Booking.events), selectinload(Booking.lines)
            )
        )
    ).scalar_one()
    booking.payment_status = BookingPaymentStatus.DEPOSIT_PAID
    reason = f"mpesa:{payment.mpesa_receipt}" if payment.mpesa_receipt else "mpesa"

    if booking.status == BookingStatus.PENDING_PAYMENT:
        transition(booking, BookingStatus.CONFIRMED, reason=reason)
        return True

    if booking.status == BookingStatus.CANCELLED and booking.cancel_reason == REINSTATABLE_CANCEL_REASON:
        # Paid after the hold ran out: bring it back if nobody took the slot meanwhile.
        # The same lock as POST /bookings, so no new booking can slip in between check and reinstatement.
        await _lock_business_day(db, booking.business_id, booking.local_date)
        if await _still_fits(db, booking, now):
            transition(booking, BookingStatus.CONFIRMED, reason=f"late_payment {reason}")
            return True
        db.add(BookingEvent(
            booking_id=booking.id, from_status=booking.status, to_status=booking.status,
            reason="late_payment_refund_due",
        ))
        from app.modules.booking.bookings import record_refund
        record_refund(booking, fee=0, refund=payment.amount)  # the business refunds it by hand (D10)
        logger.error(
            f"Booking {booking.reference} was paid ({payment.mpesa_receipt}) after its hold expired and the "
            f"slot was taken: the deposit of KES {payment.amount} needs refunding"
        )
        return False

    logger.warning(f"Deposit paid for booking {booking.reference} in status {booking.status}; nothing to change")
    return False


async def _still_fits(db: AsyncSession, booking: Booking, now: datetime) -> bool:
    slots = AvailabilityService(db, now)
    service_ids = [line.service_id for line in booking.lines if line.service_id]
    ctx = await slots.context(str(booking.business_id), service_ids, None, for_existing_booking=True)
    start, end = _aware(booking.starts_at), _aware(booking.ends_at)
    others = await slots.existing_bookings(ctx, start - timedelta(days=1), end + timedelta(days=1), exclude_id=booking.id)
    return availability.booking_fits(
        start=start, end=end, staff_id=booking.staff_id, staff=ctx.staff, bookings=others,
        service_ids=set(service_ids), settings=ctx.settings, now=now,
    )
