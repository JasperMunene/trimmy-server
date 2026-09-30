"""A business's customer list, built from its bookings plus the owner's own entries."""
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.phone_utils import InvalidPhoneNumberError, normalise_phone
from app.modules.auth.models import User
from app.modules.booking.models import Booking, BookingStatus
from app.modules.customers.models import BusinessCustomer
from app.modules.customers.schemas import CustomerResponse, ListCustomersResponse, SaveCustomerRequest

MAX_PAGE = 500


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def clean_phone(raw: Optional[str]) -> Optional[str]:
    """E.164, or None when empty. Raises HTTP 400 for something that isn't a phone number."""
    if raw is None or not raw.strip():
        return None
    try:
        return normalise_phone(raw)
    except InvalidPhoneNumberError:
        raise _error(400, "invalid_phone", "Enter a valid phone number, e.g. 0712 345 678")


async def remember_customer(
    db: AsyncSession, business_id: uuid_pkg.UUID, name: Optional[str], phone: Optional[str], email: Optional[str]
) -> None:
    """Add or refresh the business's customer record for someone who just booked. The caller commits.

    Only for bookings with a phone: that's what deduplicates people. Doesn't overwrite a
    name or email the owner already has with a blank or placeholder.
    """
    if not phone:
        return
    existing = (
        await db.execute(select(BusinessCustomer).where(BusinessCustomer.business_id == business_id, BusinessCustomer.phone == phone))
    ).scalar_one_or_none()
    if existing is None:
        db.add(BusinessCustomer(business_id=business_id, name=name or "Customer", phone=phone, email=email))
        return
    if name and name != "Walk-in customer":
        existing.name = name
    if email:
        existing.email = email


class CustomersService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _require_manager(self, user: User, business_id: str) -> uuid_pkg.UUID:
        from app.modules.booking.business_bookings import FRONT_DESK, OWNER, BusinessBookingsService

        try:
            bid = uuid_pkg.UUID(str(business_id))
        except ValueError:
            raise _error(404, "not_found", "No business found for this id")
        role, _ = await BusinessBookingsService(self.db)._role(user, bid)
        if role is None:
            raise _error(404, "not_found", "No business found for this id")
        if role not in (OWNER, FRONT_DESK):
            raise _error(403, "forbidden", "Only the owner or front desk can do this")
        return bid

    async def save(self, user: User, business_id: str, data: SaveCustomerRequest) -> CustomerResponse:
        bid = await self._require_manager(user, business_id)
        phone = clean_phone(data.phone)
        email = (data.email or "").strip() or None

        customer = None
        if data.customer_id:
            customer = await self.db.get(BusinessCustomer, data.customer_id)
            if customer is None or customer.business_id != bid:
                raise _error(404, "not_found", "customerId does not exist for this business")
        elif phone:
            customer = (
                await self.db.execute(
                    select(BusinessCustomer).where(BusinessCustomer.business_id == bid, BusinessCustomer.phone == phone)
                )
            ).scalar_one_or_none()

        if phone and (customer is None or customer.phone != phone):
            clash = (
                await self.db.execute(
                    select(BusinessCustomer.id).where(BusinessCustomer.business_id == bid, BusinessCustomer.phone == phone)
                )
            ).scalar_one_or_none()
            if clash and (customer is None or clash != customer.id):
                raise _error(409, "phone_taken", "Another customer already has this phone number")

        if customer is None:
            customer = BusinessCustomer(id=uuid_pkg.uuid4(), business_id=bid, name=data.name.strip(), phone=phone, email=email)
            self.db.add(customer)
        else:
            customer.name, customer.phone, customer.email = data.name.strip(), phone, email
        await self.db.commit()
        await self.db.refresh(customer)
        return (await self._with_stats(bid, [customer]))[0]

    async def list_customers(self, user: User, business_id: str, query: Optional[str], limit: int, offset: int) -> ListCustomersResponse:
        bid = await self._require_manager(user, business_id)
        if not 1 <= limit <= MAX_PAGE:
            raise _error(400, "validation_error", f"limit must be between 1 and {MAX_PAGE}")

        base = select(BusinessCustomer).where(BusinessCustomer.business_id == bid)
        if query and query.strip():
            q = f"%{query.strip().lower()}%"
            digits = "".join(ch for ch in query if ch.isdigit())
            conditions = [func.lower(BusinessCustomer.name).like(q), func.lower(BusinessCustomer.email).like(q)]
            if digits:
                conditions.append(BusinessCustomer.phone.like(f"%{digits.lstrip('0')}%"))
            base = base.where(or_(*conditions))
        total = (await self.db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        rows = (
            await self.db.execute(base.order_by(func.lower(BusinessCustomer.name), BusinessCustomer.id).limit(limit).offset(offset))
        ).scalars().all()
        return ListCustomersResponse(customers=await self._with_stats(bid, rows), total=total)

    async def _with_stats(self, business_id: uuid_pkg.UUID, customers: list[BusinessCustomer]) -> list[CustomerResponse]:
        phones = [c.phone for c in customers if c.phone]
        stats: dict = {}
        if phones:
            rows = await self.db.execute(
                select(Booking.customer_phone, func.count(), func.max(Booking.starts_at))
                .where(Booking.business_id == business_id, Booking.customer_phone.in_(phones),
                       Booking.status != BookingStatus.CANCELLED)
                .group_by(Booking.customer_phone)
            )
            stats = {phone: (count, last) for phone, count, last in rows.all()}

        def aware(dt: Optional[datetime]) -> Optional[datetime]:
            if dt is None:
                return None
            if isinstance(dt, str):  # SQLite aggregate results come back as text
                dt = datetime.fromisoformat(dt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        return [
            CustomerResponse(
                customerId=c.id, businessId=c.business_id, name=c.name, phone=c.phone, email=c.email,
                createdAt=aware(c.created_at),
                bookingsCount=stats.get(c.phone, (0, None))[0],
                lastBookingAt=aware(stats.get(c.phone, (0, None))[1]),
            )
            for c in customers
        ]
