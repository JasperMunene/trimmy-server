"""Which payments settled which bookings. A payment covers its own booking, or, for a visit
checkout, every booking in its allocations. Everything that reads a booking's payments (booking
responses, receipts, earnings) goes through here so visit payments count for each booking."""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import Payment, PaymentAllocation, PaymentStatus


@dataclass
class ChargePart:
    booking_id: object
    payment: Payment
    amount: int  # this booking's part of the payment, tip included
    tip: int


async def payment_parts(db: AsyncSession, booking_ids, *, succeeded_only: bool = False) -> list[ChargePart]:
    """Every payment touching these bookings, split per booking, oldest first. Two queries."""
    ids = list(booking_ids)
    if not ids:
        return []
    direct = select(Payment).where(Payment.booking_id.in_(ids), Payment.visit_id.is_(None))
    shared = (
        select(PaymentAllocation, Payment)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(PaymentAllocation.booking_id.in_(ids))
    )
    if succeeded_only:
        direct = direct.where(Payment.status == PaymentStatus.SUCCEEDED)
        shared = shared.where(Payment.status == PaymentStatus.SUCCEEDED)
    parts = [ChargePart(p.booking_id, p, p.amount, p.tip_amount or 0) for p in (await db.execute(direct)).scalars()]
    parts += [
        ChargePart(a.booking_id, p, a.amount + (a.tip_amount or 0), a.tip_amount or 0)
        for a, p in (await db.execute(shared)).all()
    ]
    parts.sort(key=lambda part: _aware(part.payment.created_at))
    return parts


def _aware(when: datetime) -> datetime:
    # SQLite hands back naive datetimes; a payment made in this session is aware.
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
