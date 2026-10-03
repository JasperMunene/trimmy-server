"""Staff payouts: balances from paid bookings, requests, and the owner's answer."""
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.business.models import Business, Staff
from app.modules.payouts.models import Payout, PayoutStatus
from app.modules.payouts.schemas import ListPayoutsResponse, PayoutBalanceResponse, PayoutResponse


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def _response(p: Payout) -> PayoutResponse:
    return PayoutResponse(
        payoutId=p.id, businessId=p.business_id, staffId=p.staff_id, staffName=p.staff_name, amount=p.amount,
        currency=p.currency, status=p.status, requestedAt=_aware(p.requested_at), respondedAt=_aware(p.responded_at),
        rejectionReason=p.rejection_reason,
    )


class PayoutsService:
    def __init__(self, db: AsyncSession, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.now(timezone.utc)

    # ─── who is asking ───────────────────────────────────────

    async def _business_role(self, user: User, business_id: uuid_pkg.UUID):
        from app.modules.booking.business_bookings import BusinessBookingsService

        return await BusinessBookingsService(self.db, self.now)._role(user, business_id)

    @staticmethod
    def _uuid(raw, message: str) -> uuid_pkg.UUID:
        try:
            return uuid_pkg.UUID(str(raw))
        except ValueError:
            raise _error(404, "not_found", message)

    # ─── balance ─────────────────────────────────────────────

    async def balance_for(self, staff: Staff) -> PayoutBalanceResponse:
        from app.modules.payouts.wallet import WalletService

        return await WalletService(self.db, self.now).balance_for(staff)

    async def my_balance(self, user: User, business_id: str) -> PayoutBalanceResponse:
        bid = self._uuid(business_id, "No business found for this id")
        _, me = await self._business_role(user, bid)
        if me is None:
            raise _error(404, "not_found", "You don't work at this business")
        return await self.balance_for(me)

    async def staff_balance(self, user: User, business_id: str, staff_id: str) -> PayoutBalanceResponse:
        from app.modules.booking.business_bookings import OWNER

        bid = self._uuid(business_id, "No business found for this id")
        sid = self._uuid(staff_id, "staffId does not exist for this business")
        role, me = await self._business_role(user, bid)
        staff = await self.db.get(Staff, sid)
        if role is None or staff is None or staff.business_id != bid:
            raise _error(404, "not_found", "staffId does not exist for this business")
        if role != OWNER and (me is None or me.id != sid):
            raise _error(403, "forbidden", "Only the owner or the staff member themselves can see this")
        return await self.balance_for(staff)

    # ─── requests ────────────────────────────────────────────

    async def request(self, user: User, business_id: uuid_pkg.UUID, staff_id: Optional[uuid_pkg.UUID], amount: int) -> PayoutResponse:
        role, me = await self._business_role(user, business_id)
        if role is None or me is None:
            raise _error(404, "not_found", "You don't work at this business")
        if staff_id is not None and staff_id != me.id:
            raise _error(403, "forbidden", "You can only request payouts for yourself")

        if self.db.bind.dialect.name == "postgresql":
            # Serialise one person's requests so two at once can't both spend the same balance.
            await self.db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"payout:{me.id}"})
        balance = await self.balance_for(me)
        if amount > balance.available:
            raise _error(409, "insufficient_balance", f"Amount exceeds available wallet balance (KES {balance.available:,})")

        payout = Payout(
            id=uuid_pkg.uuid4(), business_id=business_id, staff_id=me.id, staff_name=me.name,
            amount=amount, requested_by=user.id,
        )
        self.db.add(payout)
        await self.db.commit()
        await self.db.refresh(payout)
        return _response(payout)

    async def list_for_business(
        self, user: User, business_id: str, status: Optional[str], staff_id: Optional[uuid_pkg.UUID]
    ) -> ListPayoutsResponse:
        from app.modules.booking.business_bookings import OWNER

        bid = self._uuid(business_id, "No business found for this id")
        role, me = await self._business_role(user, bid)
        if role is None:
            raise _error(404, "not_found", "No business found for this id")
        query = select(Payout).where(Payout.business_id == bid).order_by(Payout.requested_at.desc(), Payout.id)
        if role != OWNER:
            if me is None:
                raise _error(404, "not_found", "No business found for this id")
            query = query.where(Payout.staff_id == me.id)  # staff see their own history
        elif staff_id:
            query = query.where(Payout.staff_id == staff_id)
        if status:
            if status not in {s.value for s in PayoutStatus}:
                raise _error(400, "validation_error", "status must be pending, paid or rejected")
            query = query.where(Payout.status == status)
        return ListPayoutsResponse(payouts=[_response(p) for p in (await self.db.execute(query)).scalars()])

    async def respond(self, user: User, payout_id: str, action: str, rejection_reason: Optional[str]) -> PayoutResponse:
        """Owner only. approve = "I've paid them myself"; no money moves through Trimmy."""
        not_found = _error(404, "not_found", "payoutId does not exist for this business")
        payout = await self.db.get(Payout, self._uuid(payout_id, "payoutId does not exist for this business"))
        if payout is None:
            raise not_found
        business = await self.db.get(Business, payout.business_id)
        if business is None or business.owner_id != user.id:
            raise not_found
        if payout.status != PayoutStatus.PENDING:
            raise _error(409, "already_responded", "This payout has already been approved or rejected")

        payout.status = PayoutStatus.PAID if action == "approve" else PayoutStatus.REJECTED
        payout.rejection_reason = ((rejection_reason or "").strip() or None) if action == "reject" else None
        payout.responded_by, payout.responded_at = user.id, self.now
        await self.db.commit()
        await self.db.refresh(payout)

        staff = await self.db.get(Staff, payout.staff_id) if payout.staff_id else None
        if staff is not None and staff.user_id:
            from app.modules.notifications.tasks import notify
            approved = payout.status == PayoutStatus.PAID
            await notify(
                staff.user_id,
                "Payout approved" if approved else "Payout declined",
                f"KES {payout.amount:,} " + ("marked as paid by the owner." if approved else f"not approved{': ' + payout.rejection_reason if payout.rejection_reason else '.'}"),
                type="payout_responded", payoutId=payout.id, businessId=payout.business_id,
            )
        return _response(payout)
