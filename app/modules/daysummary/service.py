"""Front desk end-of-day summary and sign-off (F4)."""
import uuid as uuid_pkg
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.auth.models import User
from app.modules.booking.bookings import deposit_paid
from app.modules.booking.models import Booking, BookingStatus, PaymentStatus as BookingPaymentStatus, RefundStatus
from app.modules.business.models import Business
from app.modules.daysummary.models import DaySubmission
from app.modules.daysummary.schemas import DaySummaryResponse
from app.modules.payments.models import Payment, PaymentKind, PaymentStatus


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


class DaySummaryService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _business(self, user: User, business_id: str) -> tuple[Business, Optional[object]]:
        from app.modules.booking.business_bookings import FRONT_DESK, OWNER, BusinessBookingsService

        try:
            bid = uuid_pkg.UUID(str(business_id))
        except ValueError:
            raise _error(404, "not_found", "No business found for this id")
        role, me = await BusinessBookingsService(self.db)._role(user, bid)
        if role is None:
            raise _error(404, "not_found", "No business found for this id")
        if role not in (OWNER, FRONT_DESK):
            raise _error(403, "forbidden", "Only the owner or front desk can do this")
        return await self.db.get(Business, bid), me

    async def summary(self, user: User, business_id: str, day: date) -> DaySummaryResponse:
        business, _ = await self._business(user, business_id)
        return await self._build(business, day)

    async def submit(self, user: User, business_id: str, day: date, notes: Optional[str]) -> DaySummaryResponse:
        business, me = await self._business(user, business_id)
        tz = ZoneInfo(business.timezone)
        if day > datetime.now(tz).date():
            raise _error(400, "validation_error", "You can't submit a day that hasn't happened yet")

        name = (getattr(me, "name", None) or " ".join(p for p in (user.first_name, user.last_name) if p) or user.email or "Front desk")
        existing = (
            await self.db.execute(select(DaySubmission).where(DaySubmission.business_id == business.id, DaySubmission.day == day))
        ).scalar_one_or_none()
        cleaned = (notes or "").strip() or None
        if existing is None:
            self.db.add(DaySubmission(business_id=business.id, day=day, submitted_by=user.id, submitted_by_name=name, notes=cleaned))
        else:  # resubmitting replaces the note
            existing.submitted_by, existing.submitted_by_name = user.id, name
            existing.submitted_at, existing.notes = datetime.now(timezone.utc), cleaned
        await self.db.commit()

        result = await self._build(business, day)
        if business.owner_id != user.id:
            await self._tell_owner(business, day, name, result, cleaned)
        return result

    async def _tell_owner(self, business: Business, day: date, name: str, s: DaySummaryResponse, notes: Optional[str]) -> None:
        from app.modules.notifications.tasks import notify, send_sms

        total = s.collected.cash.amount + s.collected.mpesa.amount
        text = (
            f"Trimmy: {name} submitted {business.name}'s day ({day.strftime('%a %-d %b')}): KES {total:,} collected "
            f"(cash {s.collected.cash.amount:,}, M-Pesa {s.collected.mpesa.amount:,}), {s.completed} completed, "
            f"{len(s.unpaid)} unpaid." + (f" Note: {notes}" if notes else "")
        )
        owner = await self.db.get(User, business.owner_id)
        if owner is not None and owner.phone:
            await send_sms(owner.phone, text[:459])
        await notify(business.owner_id, "Day summary submitted", text[8:200], type="day_summary_submitted",
                     businessId=business.id, date=day.isoformat())

    async def _build(self, business: Business, day: date) -> DaySummaryResponse:
        tz = ZoneInfo(business.timezone)
        lo = datetime.combine(day, time(0, 0), tz).astimezone(timezone.utc)
        hi = datetime.combine(day + timedelta(days=1), time(0, 0), tz).astimezone(timezone.utc)

        bookings = (
            await self.db.execute(
                select(Booking)
                .where(Booking.business_id == business.id, Booking.starts_at >= lo, Booking.starts_at < hi)
                .options(selectinload(Booking.events))
                .order_by(Booking.starts_at)
            )
        ).scalars().all()

        def is_walk_in(b: Booking) -> bool:
            first = min(b.events, key=lambda e: _aware(e.at), default=None)
            return first is not None and first.reason == "walk_in"

        live = [b for b in bookings if b.status != BookingStatus.CANCELLED]
        walk_ins = sum(1 for b in live if is_walk_in(b))

        # The drawer: payments that succeeded today, whichever day the visit is.
        paid_today = (
            await self.db.execute(
                select(Payment)
                .join(Booking, Booking.id == Payment.booking_id)
                .where(Booking.business_id == business.id, Payment.status == PaymentStatus.SUCCEEDED,
                       Payment.settled_at >= lo, Payment.settled_at < hi)
            )
        ).scalars().all()

        def tally(items) -> dict:
            items = list(items)
            return {"amount": sum(p.amount for p in items), "count": len(items)}

        refunded = (
            await self.db.execute(
                select(Booking.refund_amount).where(
                    Booking.business_id == business.id, Booking.refund_status == RefundStatus.REFUNDED,
                    Booking.refunded_at >= lo, Booking.refunded_at < hi,
                )
            )
        ).scalars().all()

        unpaid = [
            {
                "bookingId": b.id, "customerName": b.customer_name, "time": b.local_time, "status": b.status,
                "balanceDue": {"amount": max(b.total_amount - deposit_paid(b), 0), "currency": b.currency},
            }
            for b in bookings
            if b.status in (BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED) and b.payment_status != BookingPaymentStatus.PAID
        ]

        submission = (
            await self.db.execute(select(DaySubmission).where(DaySubmission.business_id == business.id, DaySubmission.day == day))
        ).scalar_one_or_none()
        return DaySummaryResponse(
            date=day,
            timezone=business.timezone,
            appointments=len(live) - walk_ins,
            walkIns=walk_ins,
            completed=sum(1 for b in bookings if b.status == BookingStatus.COMPLETED),
            noShows=sum(1 for b in bookings if b.status == BookingStatus.NO_SHOW),
            cancelled=sum(1 for b in bookings if b.status == BookingStatus.CANCELLED),
            collected={
                "cash": tally(p for p in paid_today if p.provider == "cash"),
                "mpesa": tally(p for p in paid_today if p.provider == "mpesa"),
                "depositsCollected": tally(p for p in paid_today if p.kind == PaymentKind.DEPOSIT),
                "balancePayments": tally(p for p in paid_today if p.kind == PaymentKind.CHARGE),
            },
            refundsPaid={"amount": sum(a or 0 for a in refunded), "count": len(refunded)},
            unpaid=unpaid,
            submission={
                "submittedAt": _aware(submission.submitted_at) if submission else None,
                "submittedBy": submission.submitted_by_name if submission else None,
                "notes": submission.notes if submission else None,
            },
        )
