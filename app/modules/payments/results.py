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
from app.modules.notifications.after_commit import notify_after_commit
from app.modules.payments.models import Payment, PaymentKind, PaymentStatus
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
    if result.status in (PaymentStatus.FAILED, PaymentStatus.TIMEOUT) and payment.kind == PaymentKind.DEPOSIT:
        # They may have left the payment screen: tell them it didn't go through while there's time to retry.
        from app.modules.booking.tasks import push_update
        notify_after_commit(db, lambda bid=payment.booking_id: push_update(bid, "payment_failed"))
    if result.status == PaymentStatus.SUCCEEDED:
        if result.amount is not None and result.amount != payment.amount:
            logger.error(f"Payment {payment.id}: M-Pesa reports KES {result.amount}, expected {payment.amount}")
        if payment.kind == PaymentKind.CHARGE:
            await _charge_paid(db, payment)
        else:
            confirmed = await _deposit_paid(db, payment, now)
    booking_id = payment.booking_id
    await db.commit()
    if confirmed:
        from app.modules.booking.tasks import notify_confirmed, push_update
        await notify_confirmed(booking_id)
        await push_update(booking_id, "created")  # the deposit landed: now it's a booking for the business
        await push_update(booking_id, "assigned")  # and for its staff member (not told while it was unpaid)
    return payment


async def _charge_paid(db: AsyncSession, payment: Payment) -> None:
    """The checkout balance arrived: complete the visit and mark it paid."""
    from app.modules.booking.bookings import record_refund
    from app.modules.payments.service import mark_charged

    if payment.visit_id is not None:
        await _visit_charge_paid(db, payment)
        return

    booking = (
        await db.execute(select(Booking).where(Booking.id == payment.booking_id).options(selectinload(Booking.events)))
    ).scalar_one()
    if booking.payment_status == BookingPaymentStatus.PAID:
        # Settled some other way meanwhile (e.g. cash): the customer paid twice.
        record_refund(booking, fee=0, refund=payment.amount)
        _tell_refund(db, booking.id, payment.amount, "You were charged twice for this visit")
        logger.error(
            f"Booking {booking.reference} was already paid when M-Pesa charge {payment.mpesa_receipt} "
            f"(KES {payment.amount}) arrived; refund due"
        )
        return
    await mark_charged(db, booking, reason=f"mpesa:{payment.mpesa_receipt}" if payment.mpesa_receipt else "mpesa",
                       tip=payment.tip_amount or 0)


async def _visit_charge_paid(db: AsyncSession, payment: Payment) -> None:
    """One payment for a whole visit: settle each booking with its share and its staff member's tip."""
    from app.modules.booking.bookings import record_refund
    from app.modules.payments.models import PaymentAllocation
    from app.modules.payments.service import mark_charged

    parts = (
        await db.execute(select(PaymentAllocation).where(PaymentAllocation.payment_id == payment.id))
    ).scalars().all()
    query = (
        select(Booking).where(Booking.id.in_([a.booking_id for a in parts]))
        .options(selectinload(Booking.events)).order_by(Booking.id)
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    bookings = {b.id: b for b in (await db.execute(query)).scalars()}
    reason = f"mpesa:{payment.mpesa_receipt}" if payment.mpesa_receipt else "mpesa"
    for part in parts:
        booking = bookings.get(part.booking_id)
        if booking is None:
            continue
        if booking.payment_status == BookingPaymentStatus.PAID:
            # That part was settled another way meanwhile (e.g. cash): its share is owed back.
            refund = part.amount + (part.tip_amount or 0)
            record_refund(booking, fee=0, refund=refund)
            _tell_refund(db, booking.id, refund, "You were charged twice for part of this visit")
            logger.error(
                f"Booking {booking.reference} was already paid when visit charge {payment.mpesa_receipt} "
                f"arrived; KES {refund} refund due"
            )
            continue
        await mark_charged(db, booking, reason=reason, tip=part.tip_amount or 0)


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
        _tell_refund(db, booking.id, payment.amount, "Your payment arrived after the booking had expired and the time was taken")
        logger.error(
            f"Booking {booking.reference} was paid ({payment.mpesa_receipt}) after its hold expired and the "
            f"slot was taken: the deposit of KES {payment.amount} needs refunding"
        )
        return False

    logger.warning(f"Deposit paid for booking {booking.reference} in status {booking.status}; nothing to change")
    return False


def _tell_refund(db: AsyncSession, booking_id, amount: int, why: str) -> None:
    """The customer is owed money back and the business has to send it (D10): tell both, once this commits."""
    from app.modules.booking.tasks import push_update

    notify_after_commit(db, lambda: push_update(booking_id, "refund_due", amount=amount, why=why))
    notify_after_commit(db, lambda: push_update(booking_id, "refund_owed", amount=amount, why=why))


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
