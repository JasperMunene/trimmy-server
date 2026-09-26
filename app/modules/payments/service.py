"""Starting deposit payments (BK-31). Results arrive via the callback (BK-32) or reconciliation (BK-33)."""
import logging
import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.booking.bookings import expire_holds
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.service import _aware
from app.modules.business.models import PaymentDestination, VerificationStatus
from app.modules.payments.models import Payment, PaymentKind, PaymentStatus
from app.modules.payments.mpesa import (
    MpesaClient,
    MpesaConfigError,
    MpesaError,
    PaymentProvider,
    normalise_mpesa_phone,
    route_for,
)
from app.modules.payments.schemas import StartPaymentResponse

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# A pending attempt blocks a new one this long (the customer may still be entering their PIN).
PENDING_LOCK = timedelta(seconds=120)


def _error(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message, **extra})


@lru_cache
def get_provider() -> PaymentProvider:
    """The configured provider, shared so its OAuth token is cached. Tests replace this."""
    return MpesaClient.from_settings()


class PaymentsService:
    def __init__(self, db: AsyncSession, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.now(timezone.utc)

    async def start_deposit(self, user: User, booking_id: str, phone: Optional[str]) -> StartPaymentResponse:
        not_found = _error(404, "not_found", "No booking found for this id")
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            raise not_found
        booking = await self.db.get(Booking, bid)
        if booking is None or booking.customer_id != user.id:
            raise not_found

        if booking.status == BookingStatus.PENDING_PAYMENT and _aware(booking.expires_at) <= self.now:
            await expire_holds(self.db, self.now, business_id=booking.business_id)
            await self.db.commit()
            raise _error(409, "hold_expired", "This booking's hold ran out; please book again")
        if booking.status != BookingStatus.PENDING_PAYMENT or not booking.deposit_amount:
            raise _error(409, "no_deposit_due", f"This booking has no deposit to pay (status {booking.status})")

        attempts = (
            await self.db.execute(select(Payment).where(Payment.booking_id == booking.id).order_by(Payment.created_at))
        ).scalars().all()
        if len(attempts) >= MAX_ATTEMPTS:
            raise _error(429, "too_many_attempts", "Too many payment attempts for this booking")
        latest = attempts[-1] if attempts else None
        if latest and latest.status == PaymentStatus.SUCCEEDED:
            raise _error(409, "already_paid", "The deposit is already paid")
        if latest and latest.status == PaymentStatus.PENDING and self.now - _aware(latest.created_at) < PENDING_LOCK:
            raise _error(
                409, "payment_in_progress", "An M-Pesa prompt was just sent; complete or cancel it on your phone first",
                paymentId=str(latest.id),
            )

        destination = (
            await self.db.execute(select(PaymentDestination).where(PaymentDestination.business_id == booking.business_id))
        ).scalar_one_or_none()
        if destination is None or destination.verification_status != VerificationStatus.VERIFIED:
            raise _error(409, "business_cannot_take_payments", "This business can't take M-Pesa payments right now")

        try:
            msisdn = normalise_mpesa_phone(phone or booking.customer_phone or "")
            route = route_for(destination, booking.reference)
        except MpesaError as e:
            if e.code == "invalid_phone":
                raise _error(400, "invalid_phone", e.message)
            logger.error(f"Business {booking.business_id} has an unusable payment destination: {e.message}")
            raise _error(409, "business_cannot_take_payments", "This business can't take M-Pesa payments right now")
        try:
            provider = get_provider()
        except MpesaConfigError as e:
            logger.error(f"M-Pesa is not configured: {e.message}")
            raise _error(503, "payments_unavailable", "Payments are temporarily unavailable")

        payment = Payment(
            id=uuid_pkg.uuid4(), booking_id=booking.id, kind=PaymentKind.DEPOSIT, amount=booking.deposit_amount,
            currency=booking.currency, phone=msisdn, destination_type=destination.type,
            party_b=route.party_b, account_reference=route.account_reference,
        )
        self.db.add(payment)
        await self.db.commit()  # recorded before the push, so a crash mid-call still leaves a trace

        try:
            push = await provider.request_payment(
                destination, phone=msisdn, amount=booking.deposit_amount,
                reference=booking.reference, description="Deposit",
            )
        except MpesaError as e:
            payment.status, payment.failure_reason = PaymentStatus.FAILED, "provider_error"
            payment.result_desc = f"[{e.code}] {e.message}"[:500]
            payment.settled_at = self.now
            await self.db.commit()
            logger.warning(f"STK push for booking {booking.reference} failed: [{e.code}] {e.message}")
            raise _error(502, "payment_provider_error", "M-Pesa couldn't send the prompt; please try again")

        payment.checkout_request_id = push.checkout_request_id
        payment.merchant_request_id = push.merchant_request_id
        await self.db.commit()
        return StartPaymentResponse(
            paymentId=payment.id, bookingId=booking.id, status=payment.status, amount=payment.amount,
            currency=payment.currency, phone=msisdn, customerMessage=push.customer_message,
            expiresAt=_aware(booking.expires_at),
        )
