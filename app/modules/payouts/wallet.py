"""Staff wallets: balances with adjustments, the statement, and one wallet across businesses.

Everything here is worked out on read from three sources: commission fixed on paid bookings,
wallet adjustments (tips, bonuses, deductions, advances) and payouts. Pending and paid payouts
count against the balance; rejected ones don't.
"""
import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.booking.models import Booking, PaymentStatus as BookingPaymentStatus
from app.modules.business.models import Business, Staff
from app.modules.payouts.models import AdjustmentKind, Payout, PayoutStatus, WalletAdjustment
from app.modules.payouts.schemas import (
    AdjustmentResponse,
    PayoutBalanceResponse,
    WalletBusinessBalance,
    WalletEntry,
    WalletResponse,
    WalletStatementResponse,
)

STATEMENT_MAX_LIMIT = 100
# Same-moment entries: money in before money out, so the running balance never dips needlessly.
_ORDER = {"commission": 0, "tip": 1, "bonus": 2, "deduction": 3, "advance": 4, "payout": 5}


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def adjustment_response(a: WalletAdjustment, avatar_url: Optional[str] = None) -> AdjustmentResponse:
    return AdjustmentResponse(
        adjustmentId=a.id, businessId=a.business_id, staffId=a.staff_id, staffName=a.staff_name,
        staffAvatarUrl=avatar_url, kind=a.kind,
        amount=a.amount, signedAmount=a.signed_amount, currency=a.currency, note=a.note, bookingId=a.booking_id,
        createdAt=_aware(a.created_at), voidedAt=_aware(a.voided_at),
    )


# ─── the statement (pure) ────────────────────────────────────


@dataclass(frozen=True)
class Movement:
    at: datetime
    type: str
    amount: int  # signed effect on the balance
    key: str  # entryId, also the last tie-breaker
    business_id: uuid_pkg.UUID
    description: str
    booking_id: Optional[uuid_pkg.UUID] = None
    booking_reference: Optional[str] = None
    payout_id: Optional[uuid_pkg.UUID] = None
    payout_status: Optional[str] = None
    payout_amount: Optional[int] = None
    adjustment_id: Optional[uuid_pkg.UUID] = None
    note: Optional[str] = None


def running_balances(movements: Iterable[Movement]) -> List[tuple]:
    """[(movement, balance_after)] oldest first."""
    out, balance = [], 0
    for m in sorted(movements, key=lambda m: (m.at, _ORDER[m.type], m.key)):
        balance += m.amount
        out.append((m, balance))
    return out


# ─── service ─────────────────────────────────────────────────


class WalletService:
    def __init__(self, db: AsyncSession, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.now(timezone.utc)

    async def _role(self, user: User, business_id: uuid_pkg.UUID):
        from app.modules.booking.business_bookings import BusinessBookingsService

        return await BusinessBookingsService(self.db, self.now)._role(user, business_id)

    @staticmethod
    def _uuid(raw, message: str) -> uuid_pkg.UUID:
        try:
            return uuid_pkg.UUID(str(raw))
        except ValueError:
            raise _error(404, "not_found", message)

    # ─── balances ────────────────────────────────────────────

    async def balance_for(self, staff: Staff) -> PayoutBalanceResponse:
        earned = (
            await self.db.execute(
                select(func.coalesce(func.sum(Booking.staff_commission), 0)).where(
                    Booking.staff_id == staff.id, Booking.payment_status == BookingPaymentStatus.PAID
                )
            )
        ).scalar_one()
        adjustments = dict(
            (
                await self.db.execute(
                    select(WalletAdjustment.kind, func.coalesce(func.sum(WalletAdjustment.amount), 0))
                    .where(WalletAdjustment.staff_id == staff.id, WalletAdjustment.voided_at.is_(None))
                    .group_by(WalletAdjustment.kind)
                )
            ).all()
        )
        payouts = dict(
            (
                await self.db.execute(
                    select(Payout.status, func.coalesce(func.sum(Payout.amount), 0))
                    .where(Payout.staff_id == staff.id, Payout.status.in_([PayoutStatus.PENDING, PayoutStatus.PAID]))
                    .group_by(Payout.status)
                )
            ).all()
        )
        tips, bonuses = int(adjustments.get(AdjustmentKind.TIP, 0)), int(adjustments.get(AdjustmentKind.BONUS, 0))
        deductions = int(adjustments.get(AdjustmentKind.DEDUCTION, 0))
        advances = int(adjustments.get(AdjustmentKind.ADVANCE, 0))
        pending, paid = int(payouts.get(PayoutStatus.PENDING, 0)), int(payouts.get(PayoutStatus.PAID, 0))
        return PayoutBalanceResponse(
            businessId=staff.business_id, staffId=staff.id, currency="KES", earned=int(earned),
            tips=tips, bonuses=bonuses, deductions=deductions, advances=advances, pending=pending, paid=paid,
            available=int(earned) + tips + bonuses - deductions - advances - pending - paid,
            commissionPercent=staff.commission_percent,
        )

    async def _my_staff(self, user: User) -> list:
        """[(Staff, Business)] for every business this person has a staff record at (owner's own included)."""
        return (
            await self.db.execute(
                select(Staff, Business).join(Business, Business.id == Staff.business_id)
                .where(Staff.user_id == user.id).order_by(Staff.created_at, Staff.id)
            )
        ).all()

    async def my_wallet(self, user: User) -> WalletResponse:
        rows = []
        for staff, business in await self._my_staff(user):
            b = await self.balance_for(staff)
            rows.append(WalletBusinessBalance(**b.model_dump(by_alias=True), businessName=business.name, role=staff.role))
        fields = ("earned", "tips", "bonuses", "deductions", "advances", "pending", "paid", "available")
        return WalletResponse(currency="KES", businesses=rows, **{f: sum(getattr(r, f) for r in rows) for f in fields})

    # ─── statement ───────────────────────────────────────────

    async def my_statement(self, user: User, business_id: Optional[str], limit: int, offset: int) -> WalletStatementResponse:
        mine = await self._my_staff(user)
        if business_id is not None:
            bid = self._uuid(business_id, "You don't work at this business")
            mine = [(s, b) for s, b in mine if b.id == bid]
            if not mine:
                raise _error(404, "not_found", "You don't work at this business")
        return await self._statement(mine, limit, offset)

    async def staff_statement(self, user: User, business_id: str, staff_id: str, limit: int, offset: int) -> WalletStatementResponse:
        staff, business = await self._owner_or_self(user, business_id, staff_id)
        return await self._statement([(staff, business)], limit, offset)

    async def _owner_or_self(self, user: User, business_id: str, staff_id: str):
        from app.modules.booking.business_bookings import OWNER

        bid = self._uuid(business_id, "No business found for this id")
        sid = self._uuid(staff_id, "staffId does not exist for this business")
        role, me = await self._role(user, bid)
        staff = await self.db.get(Staff, sid)
        if role is None or staff is None or staff.business_id != bid:
            raise _error(404, "not_found", "staffId does not exist for this business")
        if role != OWNER and (me is None or me.id != sid):
            raise _error(403, "forbidden", "Only the owner or the staff member themselves can see this")
        return staff, await self.db.get(Business, bid)

    async def _statement(self, scope: list, limit: int, offset: int) -> WalletStatementResponse:
        staff_ids = [s.id for s, _ in scope]
        names = {b.id: b.name for _, b in scope}
        movements: List[Movement] = []
        if staff_ids:
            bookings = (
                await self.db.execute(
                    select(Booking.id, Booking.business_id, Booking.reference, Booking.customer_name,
                           Booking.staff_commission, Booking.paid_at, Booking.updated_at)
                    .where(Booking.staff_id.in_(staff_ids), Booking.payment_status == BookingPaymentStatus.PAID,
                           Booking.staff_commission > 0)
                )
            ).all()
            for bid, business_id, reference, customer, commission, paid_at, updated_at in bookings:
                movements.append(Movement(
                    at=_aware(paid_at or updated_at), type="commission", amount=int(commission), key=f"commission:{bid}",
                    business_id=business_id, description=f"Commission on {reference}" + (f" ({customer})" if customer else ""),
                    booking_id=bid, booking_reference=reference,
                ))
            for a in (await self.db.execute(
                select(WalletAdjustment).where(WalletAdjustment.staff_id.in_(staff_ids), WalletAdjustment.voided_at.is_(None))
            )).scalars():
                movements.append(Movement(
                    at=_aware(a.created_at), type=a.kind, amount=a.signed_amount, key=f"{a.kind}:{a.id}",
                    business_id=a.business_id, description=a.kind.capitalize() + (f": {a.note}" if a.note else ""),
                    booking_id=a.booking_id, adjustment_id=a.id, note=a.note,
                ))
            for p in (await self.db.execute(select(Payout).where(Payout.staff_id.in_(staff_ids)))).scalars():
                counts = p.status in (PayoutStatus.PENDING, PayoutStatus.PAID)
                movements.append(Movement(
                    at=_aware(p.requested_at), type="payout", amount=-p.amount if counts else 0, key=f"payout:{p.id}",
                    business_id=p.business_id,
                    description={"pending": "Payout requested", "paid": "Payout", "rejected": "Payout declined"}[p.status],
                    payout_id=p.id, payout_status=p.status, payout_amount=p.amount, note=p.rejection_reason,
                ))
        ledger = running_balances(movements)
        balance = ledger[-1][1] if ledger else 0
        newest_first = list(reversed(ledger))
        page = newest_first[offset:offset + limit]
        entries = [
            WalletEntry(
                entryId=m.key, type=m.type, at=m.at, amount=m.amount, balanceAfter=after, businessId=m.business_id,
                businessName=names.get(m.business_id, ""), description=m.description, bookingId=m.booking_id,
                bookingReference=m.booking_reference, payoutId=m.payout_id, payoutStatus=m.payout_status,
                payoutAmount=m.payout_amount, adjustmentId=m.adjustment_id, note=m.note,
            )
            for m, after in page
        ]
        more = offset + limit < len(newest_first)
        return WalletStatementResponse(currency="KES", balance=balance, entries=entries, total=len(newest_first),
                                       nextOffset=offset + limit if more else None)

    # ─── adjustments ─────────────────────────────────────────

    async def add_adjustment(self, user: User, business_id: str, staff_id: str, data) -> AdjustmentResponse:
        """Owner: any kind. Front desk: tips only (handed over at checkout)."""
        from app.modules.booking.business_bookings import FRONT_DESK, OWNER

        bid = self._uuid(business_id, "No business found for this id")
        sid = self._uuid(staff_id, "staffId does not exist for this business")
        role, _ = await self._role(user, bid)
        staff = await self.db.get(Staff, sid)
        if role is None or staff is None or staff.business_id != bid:
            raise _error(404, "not_found", "staffId does not exist for this business")
        if role != OWNER and not (role == FRONT_DESK and data.kind == AdjustmentKind.TIP):
            raise _error(403, "forbidden", "Only the owner can do this (front desk can add tips)")
        if data.booking_id is not None:
            booking = await self.db.get(Booking, data.booking_id)
            if booking is None or booking.business_id != bid:
                raise _error(404, "not_found", "bookingId does not exist for this business")

        adjustment = WalletAdjustment(
            id=uuid_pkg.uuid4(), business_id=bid, staff_id=sid, staff_name=staff.name, kind=data.kind,
            amount=data.amount, note=(data.note or "").strip() or None, booking_id=data.booking_id, created_by=user.id,
        )
        self.db.add(adjustment)
        await self.db.commit()
        await self.db.refresh(adjustment)
        if staff.user_id and staff.user_id != user.id:
            from app.modules.notifications.tasks import notify

            sign = "+" if adjustment.signed_amount > 0 else "-"
            await notify(
                staff.user_id, f"{data.kind.capitalize()} recorded",
                f"{sign}KES {adjustment.amount:,} in your wallet" + (f": {adjustment.note}" if adjustment.note else "."),
                type="wallet_adjusted", adjustmentId=adjustment.id, businessId=bid,
            )
        return adjustment_response(adjustment)

    async def void_adjustment(self, user: User, adjustment_id: str) -> AdjustmentResponse:
        """Owner only: undo a mistaken adjustment. It stays on record, out of the balance."""
        not_found = _error(404, "not_found", "No adjustment found for this id")
        adjustment = await self.db.get(WalletAdjustment, self._uuid(adjustment_id, "No adjustment found for this id"))
        if adjustment is None:
            raise not_found
        business = await self.db.get(Business, adjustment.business_id)
        if business is None or business.owner_id != user.id:
            raise not_found
        if adjustment.voided_at is not None:
            raise _error(409, "already_voided", "This adjustment was already voided")
        adjustment.voided_at, adjustment.voided_by = self.now, user.id
        await self.db.commit()
        await self.db.refresh(adjustment)
        return adjustment_response(adjustment)


async def credit_checkout_tip(db: AsyncSession, booking, staff, amount: int, created_by=None) -> None:
    """A tip paid together with the checkout charge (payments/service.py): onto the staff member's
    wallet as a `tip` linked to the booking, and recorded on the booking, so the money that came in
    splits cleanly into the business's revenue and what it owes the staff member. They're told once
    the payment commits. The caller commits."""
    from app.modules.notifications.after_commit import notify_after_commit
    from app.modules.notifications.tasks import notify

    if amount <= 0:
        return
    if created_by is None:  # an M-Pesa callback: nobody's signed in; the business stands behind it
        business = await db.get(Business, booking.business_id)
        created_by = business.owner_id
    booking.tip_amount = (booking.tip_amount or 0) + amount
    db.add(WalletAdjustment(
        id=uuid_pkg.uuid4(), business_id=booking.business_id, staff_id=staff.id, staff_name=staff.name,
        kind=AdjustmentKind.TIP, amount=amount, currency=booking.currency,
        note=f"Tip from {booking.customer_name} ({booking.reference})", booking_id=booking.id, created_by=created_by,
    ))
    if staff.user_id:
        # Plain values: the ORM rows are expired by the time the commit hook runs.
        user_id, text = staff.user_id, f"{booking.customer_name} tipped you {booking.currency} {amount:,}. It's in your wallet."
        business_id, booking_id = booking.business_id, booking.id
        notify_after_commit(db, lambda: notify(
            user_id, "You got a tip", text, type="wallet_adjusted", businessId=business_id, bookingId=booking_id,
        ))
