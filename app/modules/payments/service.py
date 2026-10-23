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

    async def charge(self, user: User, booking_id: str, method: str, phone: Optional[str], tip: int = 0):
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

        if tip and booking.staff_id is None:
            raise _error(400, "tip_needs_staff", "Assign a staff member first, so the tip has someone to go to")
        if booking.visit_id is not None:
            await self._no_pending_visit_charge(booking.visit_id)

        balance = max(booking.total_amount - deposit_paid(booking), 0)
        due = balance + tip  # one payment: the balance and the tip together
        if method == "cash" or due == 0:
            if due > 0:
                self.db.add(Payment(
                    id=uuid_pkg.uuid4(), booking_id=booking.id, kind=PaymentKind.CHARGE, provider="cash",
                    amount=due, tip_amount=tip, currency=booking.currency, phone="", destination_type="cash",
                    party_b="", account_reference=booking.reference, status=PaymentStatus.SUCCEEDED, settled_at=self.now,
                ))
            await self.db.refresh(booking, ["events"])
            await mark_charged(self.db, booking, reason="cash" if due else "nothing_due", actor_id=user.id, tip=tip)
            await self.db.commit()
            return 200, await BookingsService(self.db, self.now).get_response(booking.id)

        payment, push = await self._push(
            booking, kind=PaymentKind.CHARGE, amount=due, phone=phone, description="Payment",
            already_paid="This appointment is already paid",
            busy="An M-Pesa prompt was just sent; wait for the customer to respond", tip=tip,
        )
        return 202, self._started(payment, push, expires_at=None)

    async def charge_visit(
        self, user: User, visit_id: str, method: str, phone: Optional[str], tips: list, booking_ids: Optional[list],
    ):
        """Check a multi-staff visit out in one payment: every part's balance plus each person's tip
        (the customer's split). Returns (status_code, body) like charge(): 200 with the Visit for cash
        or nothing due, 202 with the started payment for M-Pesa."""
        from app.modules.booking.bookings import deposit_paid
        from app.modules.booking.visits import CHARGEABLE, VisitsService
        from app.modules.payments.models import PaymentAllocation

        visits = VisitsService(self.db, self.now)
        visit, bookings = await visits.load(user, visit_id)
        if self.db.bind.dialect.name == "postgresql":
            # Locked in a fixed order, so two checkouts of one visit can't both take it.
            ids = sorted(b.id for b in bookings)
            await self.db.execute(select(Booking.id).where(Booking.id.in_(ids)).order_by(Booking.id).with_for_update())
        counted = [b for b in bookings if b.status not in (BookingStatus.CANCELLED, BookingStatus.NO_SHOW)]
        unpaid = [b for b in counted if b.payment_status != BookingPaymentStatus.PAID]

        if booking_ids is not None:
            wanted = {uuid_pkg.UUID(str(i)) for i in booking_ids}
            if not wanted <= {b.id for b in unpaid}:
                raise _error(400, "validation_error", "bookingIds must be unpaid bookings in this visit")
            chargeable = [b for b in unpaid if b.id in wanted]
            if any(b.status not in CHARGEABLE for b in chargeable):
                raise _error(409, "not_in_progress", "Only started or finished services can be charged")
        else:
            waiting = [b for b in unpaid if b.status not in CHARGEABLE]
            if waiting:
                raise _error(
                    409, "visit_not_ready", "Some services haven't started yet: start them, cancel them, or charge the rest",
                    bookingIds=[str(b.id) for b in waiting],
                )
            chargeable = unpaid
        if not chargeable:
            raise _error(409, "already_paid", "This visit is already paid")

        # Each person's tip goes on their first booking being charged (so it lands in their wallet with it).
        chargeable.sort(key=lambda b: (_aware(b.starts_at), str(b.id)))
        first_for: dict = {}
        for b in chargeable:
            if b.staff_id is not None:
                first_for.setdefault(b.staff_id, b.id)
        tip_for: dict = {}
        for t in tips:
            if t.staff_id in tip_for:
                raise _error(400, "validation_error", "Each staff member can only be tipped once")
            if t.amount and t.staff_id not in first_for:
                raise _error(400, "tip_staff_not_in_visit", "Tips can only go to someone whose service is being charged")
            tip_for[t.staff_id] = t.amount
        if sum(tip_for.values()) > 100_000:
            raise _error(400, "validation_error", "Tips can be at most KES 100,000 in total")
        await self._no_pending_visit_charge(visit.id)
        await self._no_pending_booking_charges([b.id for b in chargeable])

        parts = [
            (b, max(b.total_amount - deposit_paid(b), 0), tip_for.get(b.staff_id, 0) if first_for.get(b.staff_id) == b.id else 0)
            for b in chargeable
        ]
        tip_total = sum(tip for _, _, tip in parts)
        due = sum(share for _, share, _ in parts) + tip_total
        anchor = chargeable[0]

        if method == "cash" or due == 0:
            if due > 0:
                payment = Payment(
                    id=uuid_pkg.uuid4(), booking_id=anchor.id, visit_id=visit.id, kind=PaymentKind.CHARGE, provider="cash",
                    amount=due, tip_amount=tip_total, currency=anchor.currency, phone="", destination_type="cash",
                    party_b="", account_reference=anchor.reference, status=PaymentStatus.SUCCEEDED, settled_at=self.now,
                )
                self.db.add(payment)
                for b, share, tip in parts:
                    self.db.add(PaymentAllocation(payment_id=payment.id, booking_id=b.id, staff_id=b.staff_id, amount=share, tip_amount=tip))
            for b, _, tip in parts:
                await self.db.refresh(b, ["events"])
                await mark_charged(self.db, b, reason="cash" if due else "nothing_due", actor_id=user.id, tip=tip)
            await self.db.commit()
            return 200, await visits.get(user, str(visit.id))

        payment, push = await self._push(
            anchor, kind=PaymentKind.CHARGE, amount=due, phone=phone, description="Payment",
            already_paid="This visit is already paid",
            busy="An M-Pesa prompt was just sent; wait for the customer to respond", tip=tip_total,
            visit_id=visit.id, allocations=[(b.id, b.staff_id, share, tip) for b, share, tip in parts],
        )
        started = self._started(payment, push, expires_at=None)
        started.visit_id = visit.id
        return 202, started

    async def _no_pending_visit_charge(self, visit_id) -> None:
        """A visit's M-Pesa prompt still waiting for the customer blocks charging its bookings again."""
        pending = (
            await self.db.execute(
                select(Payment).where(
                    Payment.visit_id == visit_id, Payment.kind == PaymentKind.CHARGE, Payment.status == PaymentStatus.PENDING,
                ).order_by(Payment.created_at.desc())
            )
        ).scalars().first()
        if pending is not None and self.now - _aware(pending.created_at) < PENDING_LOCK:
            raise _error(409, "payment_in_progress", "An M-Pesa prompt was just sent for this visit; wait for the customer to respond",
                         paymentId=str(pending.id))

    async def _no_pending_booking_charges(self, booking_ids: list) -> None:
        pending = (
            await self.db.execute(
                select(Payment).where(
                    Payment.booking_id.in_(booking_ids), Payment.visit_id.is_(None),
                    Payment.kind == PaymentKind.CHARGE, Payment.status == PaymentStatus.PENDING,
                ).order_by(Payment.created_at.desc())
            )
        ).scalars().first()
        if pending is not None and self.now - _aware(pending.created_at) < PENDING_LOCK:
            raise _error(409, "payment_in_progress", "An M-Pesa prompt was just sent for one of these services; wait for it",
                         paymentId=str(pending.id))

    # ─── shared STK push path ────────────────────────────────

    async def _booking(self, booking_id: str) -> Optional[Booking]:
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            return None
        return await self.db.get(Booking, bid)

    async def _push(
        self, booking: Booking, *, kind: str, amount: int, phone: Optional[str], description: str,
        already_paid: str, busy: str, tip: int = 0, visit_id=None, allocations: Optional[list] = None,
    ) -> tuple[Payment, "StkPushResult"]:
        """Guards, routing and the STK push, shared by deposits and checkout charges. A visit checkout
        (visit_id) counts attempts per visit, prompts against `booking` (its first part) and records
        `allocations` [(booking_id, staff_id, amount, tip)] with the payment."""
        scope = Payment.visit_id == visit_id if visit_id is not None else Payment.booking_id == booking.id
        attempts = (
            await self.db.execute(select(Payment).where(scope, Payment.kind == kind).order_by(Payment.created_at))
        ).scalars().all()
        if len(attempts) >= MAX_ATTEMPTS:
            raise _error(429, "too_many_attempts", "Too many payment attempts for this booking")
        latest = attempts[-1] if attempts else None
        # A visit can be paid in parts, so an earlier success doesn't mean nothing's left (the caller checked).
        if latest and latest.status == PaymentStatus.SUCCEEDED and visit_id is None:
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
            id=uuid_pkg.uuid4(), booking_id=booking.id, visit_id=visit_id, kind=kind, amount=amount, tip_amount=tip,
            currency=booking.currency, phone=msisdn, destination_type=destination.type,
            party_b=route.party_b, account_reference=route.account_reference,
        )
        self.db.add(payment)
        if allocations:
            from app.modules.payments.models import PaymentAllocation
            for booking_id, staff_id, share, part_tip in allocations:
                self.db.add(PaymentAllocation(
                    payment_id=payment.id, booking_id=booking_id, staff_id=staff_id, amount=share, tip_amount=part_tip,
                ))
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


async def mark_charged(db: AsyncSession, booking: Booking, *, reason: str, actor_id=None, tip: int = 0) -> None:
    """The balance is settled: complete the visit, mark it paid, fix the staff member's commission
    on it (payouts) and credit any tip paid with it to their wallet. The caller commits."""
    import math

    from app.modules.booking.states import transition
    from app.modules.business.models import Staff

    booking.payment_status = BookingPaymentStatus.PAID
    booking.paid_at = datetime.now(timezone.utc)
    staff = await db.get(Staff, booking.staff_id) if booking.staff_id else None
    if staff is not None and staff.commission_percent and staff.role != "front_desk":
        # On the services only: a tip is all theirs, through the wallet below.
        booking.staff_commission = math.floor(booking.total_amount * staff.commission_percent / 100)
    if tip > 0 and staff is not None:
        from app.modules.payouts.wallet import credit_checkout_tip
        await credit_checkout_tip(db, booking, staff, tip, created_by=actor_id)
    if booking.status == BookingStatus.IN_PROGRESS:
        transition(booking, BookingStatus.COMPLETED, actor_id=actor_id, reason=f"charged: {reason}"[:100])
    # The receipt (and review prompt). Once per booking, however it was completed.
    from app.modules.booking.tasks import push_update
    from app.modules.notifications.after_commit import notify_after_commit
    notify_after_commit(db, lambda bid=booking.id: push_update(bid, "completed"))
