"""Visits: one customer's time at the business across several bookings (different staff),
summarised for checking out and tipping together."""
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.auth.models import User
from app.modules.booking.bookings import BookingsService
from app.modules.booking.models import Booking, BookingStatus, PaymentStatus, Visit
from app.modules.booking.schemas import (
    Money,
    VisitNotReadyResponse,
    VisitResponse,
    VisitStaffResponse,
)

# Counted in a visit's totals: everything but cancelled and no-show bookings.
_SKIPPED = (BookingStatus.CANCELLED, BookingStatus.NO_SHOW)
# Ready to be paid for: started or done.
CHARGEABLE = (BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


class VisitsService:
    def __init__(self, db: AsyncSession, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.now(timezone.utc)

    async def load(self, user: User, visit_id: str) -> tuple[Visit, list[Booking]]:
        """The visit and its bookings (soonest first), for the owner or front desk; else 404/403."""
        from app.modules.booking.business_bookings import BusinessBookingsService

        try:
            vid = uuid_pkg.UUID(str(visit_id))
        except ValueError:
            raise _error(404, "not_found", "visitId does not exist")
        visit = await self.db.get(Visit, vid)
        if visit is None:
            raise _error(404, "not_found", "visitId does not exist")
        await BusinessBookingsService(self.db, self.now)._require_manager(user, visit.business_id)
        bookings = (
            await self.db.execute(
                select(Booking).where(Booking.visit_id == vid)
                .options(selectinload(Booking.lines))
                .order_by(Booking.starts_at, Booking.id)
            )
        ).scalars().all()
        return visit, list(bookings)

    async def get(self, user: User, visit_id: str) -> VisitResponse:
        visit, bookings = await self.load(user, visit_id)
        return await self.response(visit, bookings)

    async def response(self, visit: Visit, bookings: list[Booking]) -> VisitResponse:
        # A fixed number of queries however many bookings: this is polled during checkout.
        responses = await BookingsService(self.db, self.now).to_responses(bookings)
        currency = bookings[0].currency if bookings else "KES"
        counted = [r for r in responses if r.status not in _SKIPPED]

        per_staff: dict = {}
        for r in counted:
            row = per_staff.setdefault(r.staff_id, {"name": r.staff_name, "subtotal": 0, "due": 0, "ids": []})
            row["subtotal"] += r.total_amount.amount
            row["due"] += r.balance_due.amount if r.balance_due else 0
            row["ids"].append(r.booking_id)

        total = sum(r.total_amount.amount for r in counted)
        deposit = sum(r.deposit_paid.amount for r in counted if r.deposit_paid)
        due = sum(r.balance_due.amount for r in counted if r.balance_due)
        paid = [r for r in counted if r.payment_status == PaymentStatus.PAID]
        status = "paid" if counted and len(paid) == len(counted) else "partially_paid" if paid else "unpaid"
        first = bookings[0] if bookings else None
        return VisitResponse(
            visitId=visit.id,
            businessId=visit.business_id,
            customerName=first.customer_name if first else "",
            customerPhone=first.customer_phone if first else None,
            bookings=responses,
            staff=[
                VisitStaffResponse(
                    staffId=staff_id, staffName=row["name"],
                    subtotal=Money(amount=row["subtotal"], currency=currency),
                    balanceDue=Money(amount=row["due"], currency=currency),
                    bookingIds=row["ids"],
                )
                for staff_id, row in per_staff.items()
            ],
            total=Money(amount=total, currency=currency),
            depositPaid=Money(amount=deposit, currency=currency),
            balanceDue=Money(amount=due, currency=currency),
            paymentStatus=status,
            chargeableBookingIds=[
                r.booking_id for r in counted if r.status in CHARGEABLE and r.payment_status != PaymentStatus.PAID
            ],
            notReady=[
                VisitNotReadyResponse(bookingId=r.booking_id, status=r.status, staffName=r.staff_name)
                for r in counted if r.status not in CHARGEABLE
            ],
            chargePayment=await self._latest_charge(visit.id),
        )

    async def _latest_charge(self, visit_id: uuid_pkg.UUID):
        """The latest checkout charge taken for the whole visit (POST /visits/{id}/charge)."""
        from app.modules.booking.bookings import _payment_response
        from app.modules.payments.models import Payment, PaymentKind

        latest = (
            await self.db.execute(
                select(Payment).where(Payment.visit_id == visit_id, Payment.kind == PaymentKind.CHARGE)
                .order_by(Payment.created_at.desc()).limit(1)
            )
        ).scalars().first()
        return _payment_response(latest)
