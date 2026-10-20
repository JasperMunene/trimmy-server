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
from app.modules.booking.models import Booking, BookingStatus, PaymentStatus as BookingPaymentStatus
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
        """The customer pays a pending_payment booking's deposit (BK-31)."""
        booking = await self._booking(booking_id)
        if booking is None or booking.customer_id != user.id:
            raise _error(404, "not_found", "No booking found for this id")

        if booking.status == BookingStatus.PENDING_PAYMENT and _aware(booking.expires_at) <= self.now:
            await expire_holds(self.db, self.now, business_id=booking.business_id)
            await self.db.commit()
            raise _error(409, "hold_expired", "This booking's hold ran out; please book again")
        if booking.status != BookingStatus.PENDING_PAYMENT or not booking.deposit_amount:
            raise _error(409, "no_deposit_due", f"This booking has no deposit to pay (status {booking.status})")

        payment, push = await self._push(
            booking, kind=PaymentKind.DEPOSIT, amount=booking.deposit_amount, phone=phone, description="Deposit",
            already_paid="The deposit is already paid",
            busy="An M-Pesa prompt was just sent; complete or cancel it on your phone first",
        )
        return self._started(payment, push, expires_at=_aware(booking.expires_at))

    async def charge(self, user: User, booking_id: str, method: str, phone: Optional[str]):
        """The final charge at checkout (fulfillment.json#charge-booking), owner or front desk.

        Returns (status_code, body): 200 with the completed Booking for cash (or when
        nothing is left to pay), 202 with the started payment for M-Pesa.
        """
        from app.modules.booking.bookings import BookingsService, deposit_paid
        from app.modules.booking.business_bookings import FRONT_DESK, OWNER, BusinessBookingsService

        booking = await self._booking(booking_id)
        if booking is None:
            raise _error(404, "not_found", "No booking found for this id")
        role, _ = await BusinessBookingsService(self.db, self.now)._role(user, booking.business_id)
        if role is None:
            raise _error(404, "not_found", "No booking found for this id")
        if role not in (OWNER, FRONT_DESK):
            raise _error(403, "forbidden", "Only the owner or front desk can do this")
        if booking.payment_status == BookingPaymentStatus.PAID:
            raise _error(409, "already_paid", "This appointment is already paid")
        # A visit someone already marked completed (e.g. staff finishing their own) can still be
        # charged afterwards, so it isn't left unpaid and missing from earnings and commission.
        if booking.status not in (BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED):
            raise _error(409, "not_in_progress", "Only an in-progress or completed appointment can be charged")

        balance = max(booking.total_amount - deposit_paid(booking), 0)
        if method == "cash" or balance == 0:
            if balance > 0:
                self.db.add(Payment(
                    id=uuid_pkg.uuid4(), booking_id=booking.id, kind=PaymentKind.CHARGE, provider="cash",
                    amount=balance, currency=booking.currency, phone="", destination_type="cash", party_b="",
                    account_reference=booking.reference, status=PaymentStatus.SUCCEEDED, settled_at=self.now,
                ))
            await self.db.refresh(booking, ["events"])
            await mark_charged(self.db, booking, reason="cash" if balance else "nothing_due", actor_id=user.id)
            await self.db.commit()
            return 200, await BookingsService(self.db, self.now).get_response(booking.id)

        payment, push = await self._push(
            booking, kind=PaymentKind.CHARGE, amount=balance, phone=phone, description="Payment",
            already_paid="This appointment is already paid",
            busy="An M-Pesa prompt was just sent; wait for the customer to respond",
        )
        return 202, self._started(payment, push, expires_at=None)

    # ─── shared STK push path ────────────────────────────────

    async def _booking(self, booking_id: str) -> Optional[Booking]:
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            return None
        return await self.db.get(Booking, bid)

    async def _push(
        self, booking: Booking, *, kind: str, amount: int, phone: Optional[str], description: str,
        already_paid: str, busy: str,
    ) -> tuple[Payment, "StkPushResult"]:
        """Guards, routing and the STK push, shared by deposits and checkout charges."""
        attempts = (
            await self.db.execute(
                select(Payment).where(Payment.booking_id == booking.id, Payment.kind == kind).order_by(Payment.created_at)
            )
        ).scalars().all()
        if len(attempts) >= MAX_ATTEMPTS:
            raise _error(429, "too_many_attempts", "Too many payment attempts for this booking")
        latest = attempts[-1] if attempts else None
        if latest and latest.status == PaymentStatus.SUCCEEDED:
            raise _error(409, "already_paid", already_paid)
        if latest and latest.status == PaymentStatus.PENDING and self.now - _aware(latest.created_at) < PENDING_LOCK:
            raise _error(409, "payment_in_progress", busy, paymentId=str(latest.id))

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
            id=uuid_pkg.uuid4(), booking_id=booking.id, kind=kind, amount=amount,
            currency=booking.currency, phone=msisdn, destination_type=destination.type,
            party_b=route.party_b, account_reference=route.account_reference,
        )
        self.db.add(payment)
        await self.db.commit()  # recorded before the push, so a crash mid-call still leaves a trace

        try:
            push = await provider.request_payment(
                destination, phone=msisdn, amount=amount, reference=booking.reference, description=description,
            )
        except MpesaError as e:
            payment.status, payment.failure_reason = PaymentStatus.FAILED, "provider_error"
            payment.result_desc = f"[{e.code}] {e.message}"[:500]
            payment.settled_at = self.now
            await self.db.commit()
            logger.warning(f"STK push ({kind}) for booking {booking.reference} failed: [{e.code}] {e.message}")
            raise _error(502, "payment_provider_error", "M-Pesa couldn't send the prompt; please try again")

        payment.checkout_request_id = push.checkout_request_id
        payment.merchant_request_id = push.merchant_request_id
        await self.db.commit()
        return payment, push

    @staticmethod
    def _started(payment: Payment, push, expires_at) -> StartPaymentResponse:
        return StartPaymentResponse(
            paymentId=payment.id, bookingId=payment.booking_id, status=payment.status, amount=payment.amount,
            currency=payment.currency, phone=payment.phone, customerMessage=push.customer_message,
            expiresAt=expires_at,
        )


async def mark_charged(db: AsyncSession, booking: Booking, *, reason: str, actor_id=None) -> None:
    """The balance is settled: complete the visit, mark it paid, and fix the staff
    member's commission on it (payouts). The caller commits."""
    import math

    from app.modules.booking.states import transition
    from app.modules.business.models import Staff

    booking.payment_status = BookingPaymentStatus.PAID
    booking.paid_at = datetime.now(timezone.utc)
    staff = await db.get(Staff, booking.staff_id) if booking.staff_id else None
    if staff is not None and staff.commission_percent:
        booking.staff_commission = math.floor(booking.total_amount * staff.commission_percent / 100)
    if booking.status == BookingStatus.IN_PROGRESS:
        transition(booking, BookingStatus.COMPLETED, actor_id=actor_id, reason=f"charged: {reason}"[:100])
    # The receipt (and review prompt). Once per booking, however it was completed.
    from app.modules.booking.tasks import push_update
    from app.modules.notifications.after_commit import notify_after_commit
    notify_after_commit(db, lambda bid=booking.id: push_update(bid, "completed"))
