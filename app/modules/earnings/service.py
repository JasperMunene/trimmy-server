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
from app.modules.booking.models import Booking
from app.modules.business.models import Business, Service, Staff
from app.modules.earnings import summary as agg
from app.modules.earnings.schemas import EarningsSummaryResponse
from app.modules.payments.models import Payment, PaymentKind, PaymentStatus

MAX_RANGE_DAYS = 366


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class EarningsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def summary(
        self,
        user: User,
        business_id: str,
        start_day: date,
        end_day: date,
        staff_id: Optional[uuid_pkg.UUID],
        transactions_limit: Optional[int],
    ) -> EarningsSummaryResponse:
        from app.modules.booking.business_bookings import OWNER, STAFF, BusinessBookingsService

        try:
            bid = uuid_pkg.UUID(str(business_id))
        except ValueError:
            raise _error(404, "not_found", "No business found for this id")
        role, me = await BusinessBookingsService(self.db)._role(user, bid)
        if role is None:
            raise _error(404, "not_found", "No business found for this id")
        if role == STAFF:
            # Staff only ever see their own earnings.
            if staff_id is not None and staff_id != me.id:
                raise _error(403, "forbidden", "You can only see your own earnings")
            staff_id = me.id
        elif role != OWNER:
            raise _error(403, "forbidden", "Only the owner can see the business's earnings")

        if end_day < start_day:
            raise _error(400, "validation_error", "to must be on or after from")
        if (end_day - start_day).days >= MAX_RANGE_DAYS:
            raise _error(400, "validation_error", f"from..to can be at most {MAX_RANGE_DAYS} days")
        if transactions_limit is not None and transactions_limit < 0:
            raise _error(400, "validation_error", "transactionsLimit must be 0 or more")

        business = await self.db.get(Business, bid)
        if staff_id is not None:
            member = await self.db.get(Staff, staff_id)
            if member is None or member.business_id != bid:
                raise _error(404, "not_found", "staffId does not exist for this business")

        tz = ZoneInfo(business.timezone)
        start = datetime.combine(start_day, time(0, 0), tz)
        end = datetime.combine(end_day + timedelta(days=1), time(0, 0), tz)
        prev_start, prev_end = start - (end - start), start

        rows, payments = await self._load(bid, prev_start, end, staff_id, tz)
        services = (
            await self.db.execute(
                select(Service.id, Service.name).where(Service.business_id == bid).order_by(Service.name)
            )
        ).all()

        current = agg.summarise(
            bookings=rows, payments=payments, services=[(s.id, s.name) for s in services],
            start=start, end=end, with_staff_breakdown=staff_id is None,
        )
        previous = agg.summarise(bookings=rows, payments=payments, services=[], start=prev_start, end=prev_end,
                                 with_staff_breakdown=False)

        transactions = current.transactions if transactions_limit is None else current.transactions[:transactions_limit]
        return EarningsSummaryResponse(
            totalRevenue=current.total_revenue,
            completed=current.completed,
            noShow=current.no_show,
            cancelled=current.cancelled,
            avgTicket=current.avg_ticket,
            buckets=[
                {"label": b.label, "amount": b.amount, "start": b.start, "end": b.end, "bookingsCount": b.bookings_count}
                for b in current.buckets
            ],
            paymentMethods=current.payment_methods,
            services=current.services,
            transactions=transactions,
            currency="KES",
            **{"from": start_day},
            to=end_day,
            timezone=business.timezone,
            staffId=staff_id,
            granularity=current.granularity,
            paidBookings=current.paid_bookings,
            commission=current.commission,
            transactionsTotal=len(current.transactions),
            staff=current.staff,
            money={
                "depositsCollected": current.deposits_collected,
                "cancellationFees": current.cancellation_fees,
                "refundsDue": current.refunds_due,
                "refundsPaid": current.refunds_paid,
                "collected": current.collected,
            },
            previous={
                "from": prev_start.date(),
                "to": (prev_end - timedelta(days=1)).date(),
                "totalRevenue": previous.total_revenue,
                "completed": previous.completed,
                "avgTicket": previous.avg_ticket,
                "revenueChangePercent": agg.change_percent(current.total_revenue, previous.total_revenue),
            },
        )

    async def _load(
        self, business_id: uuid_pkg.UUID, start: datetime, end: datetime, staff_id: Optional[uuid_pkg.UUID], tz: ZoneInfo
    ) -> tuple[list[agg.BookingRow], list[agg.PaymentRow]]:
        """Bookings starting in [start, end) with their lines and successful payments: three queries."""
        query = (
            select(Booking)
            .where(
                Booking.business_id == business_id,
                Booking.starts_at >= start.astimezone(timezone.utc),
                Booking.starts_at < end.astimezone(timezone.utc),
            )
            .options(selectinload(Booking.lines))
        )
        if staff_id is not None:
            query = query.where(Booking.staff_id == staff_id)
        bookings = (await self.db.execute(query)).scalars().all()
        if not bookings:
            return [], []

        ids = [b.id for b in bookings]
        succeeded = (
            await self.db.execute(
                select(Payment).where(Payment.booking_id.in_(ids), Payment.status == PaymentStatus.SUCCEEDED)
            )
        ).scalars().all()
        staff_ids = {b.staff_id for b in bookings if b.staff_id}
        names = dict(
            (await self.db.execute(select(Staff.id, Staff.name).where(Staff.id.in_(staff_ids)))).all()
        ) if staff_ids else {}

        # What settled each booking: its checkout charge, else an M-Pesa deposit.
        method: dict = {}
        for p in succeeded:
            if p.kind == PaymentKind.CHARGE:
                method[p.booking_id] = p.provider
            else:
                method.setdefault(p.booking_id, "mpesa")

        rows = [
            agg.BookingRow(
                booking_id=b.id,
                starts=_aware(b.starts_at).astimezone(tz),
                status=b.status,
                payment_status=b.payment_status,
                total=b.total_amount,
                payment_method=method.get(b.id),
                customer_name=b.customer_name,
                lines=tuple(agg.Line(l.service_id, l.name, l.price_amount * l.quantity) for l in b.lines),
                staff_id=b.staff_id,
                staff_name=names.get(b.staff_id),
                commission=b.staff_commission or 0,
                deposit_paid=deposit_paid(b),
                cancellation_fee=b.cancellation_fee or 0,
                refund_amount=b.refund_amount or 0,
                refund_status=b.refund_status,
            )
            for b in bookings
        ]
        payments = [agg.PaymentRow(p.booking_id, p.kind, p.provider, p.amount) for p in succeeded]
        return rows, payments
