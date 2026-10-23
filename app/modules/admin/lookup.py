"""Admin look-up tools: platform overview, users, businesses, bookings and payments.

Read-only, except re-checking a stuck M-Pesa payment (which goes through the same code as the
callback). Lists page with (created_at, id) cursors; detail pages load related rows in a fixed
number of batched queries.
"""
import re
import uuid as uuid_pkg
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, cache
from app.core.audit import AuditLog
from app.core.database import get_db
from app.core.dependencies import require_admin
from app.modules.admin.auth import client_ip
from app.modules.admin.schemas import (
    AuditEntry, BookingDetail, BookingEventRow, BookingLookup, BookingRow, BusinessDetail, BusinessPage,
    BusinessRow, DayPoint, FailureCount, Money, OverviewResponse, PaymentRow, ReasonRequest, TeamMember,
    UserDetail, UserPage, UserRow, Workplace,
)
from app.modules.auth.models import RefreshToken, User
from app.modules.booking.models import Booking, BookingEvent, BookingLine, BookingStatus
from app.modules.business.models import (
    Business, BusinessPolicies, BusinessStatus, PaymentDestination, Review, Service, Staff, WorkingHours,
)
from app.modules.payments.models import Payment, PaymentAllocation, PaymentStatus

router = APIRouter(prefix="/admin", tags=["Admin"])

TZ_NAME = "Africa/Nairobi"
TZ = ZoneInfo(TZ_NAME)
PAGE = 30
SERIES_DAYS = 30
OVERVIEW_TTL = 60


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _uuid(value: str, what: str = "record") -> uuid_pkg.UUID:
    try:
        return uuid_pkg.UUID(str(value))
    except ValueError:
        raise _error(404, "not_found", f"No {what} with that id")


def _val(value) -> Optional[str]:
    """An enum column's value (str enums load as members or plain strings)."""
    return None if value is None else str(value.value if hasattr(value, "value") else value)


def _aware(at: Optional[datetime]) -> Optional[datetime]:
    return at.replace(tzinfo=timezone.utc) if at is not None and at.tzinfo is None else at


def _name(first: Optional[str], last: Optional[str], fallback: Optional[str] = None) -> str:
    return " ".join(p for p in (first, last) if p) or fallback or "No name"


def _digits(q: str) -> str:
    """Phone digits to search on: 0712 345 678 / +254712345678 / 712345678 all → 712345678."""
    digits = re.sub(r"\D", "", q)
    if digits.startswith("254"):
        digits = digits[3:]
    return digits.lstrip("0")


def _cursor(at: datetime, rid) -> str:
    return f"{_aware(at).isoformat()}|{rid}"


def _after(cursor: Optional[str], at_col, id_col):
    if not cursor:
        return None
    try:
        raw_at, raw_id = cursor.split("|", 1)
        at, rid = datetime.fromisoformat(raw_at), uuid_pkg.UUID(raw_id)
    except ValueError:
        raise _error(400, "validation_error", "Bad cursor")
    return or_(at_col < at, and_(at_col == at, id_col < rid))


def _local_day(db: AsyncSession, col):
    """The Nairobi calendar day of a timestamp, as 'YYYY-MM-DD', in SQL."""
    # Literals, not bind parameters, so SELECT and GROUP BY are the same expression to Postgres.
    if db.bind.dialect.name == "postgresql":
        return func.to_char(func.timezone(literal_column(f"'{TZ_NAME}'"), col), literal_column("'YYYY-MM-DD'"))
    return func.strftime(literal_column("'%Y-%m-%d'"), col, literal_column("'+3 hours'"))


def _local_midnight(days_ago: int = 0) -> datetime:
    today = datetime.now(TZ).date() - timedelta(days=days_ago)
    return datetime.combine(today, time.min, tzinfo=TZ).astimezone(timezone.utc)


def audit_entry(r: AuditLog) -> AuditEntry:
    return AuditEntry(id=str(r.id), actorId=str(r.actor_id) if r.actor_id else None, actorName=r.actor_name,
                      action=r.action, targetType=r.target_type, targetId=r.target_id, reason=r.reason,
                      changes=r.changes, ip=r.ip, at=r.at)


async def _audit_for(db: AsyncSession, target_type: str, target_id, limit: int = 20) -> list[AuditEntry]:
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.target_type == target_type, AuditLog.target_id == str(target_id))
        .order_by(AuditLog.at.desc()).limit(limit)
    )).scalars().all()
    return [audit_entry(r) for r in rows]


def user_row(u: User) -> UserRow:
    return UserRow(id=str(u.id), name=_name(u.first_name, u.last_name, u.email or u.phone), email=u.email,
                   phone=u.phone, avatarUrl=u.avatar_url, authType=_val(u.auth_type),
                   verified=bool(u.verified), platformRole=u.platform_role, disabledAt=u.disabled_at, createdAt=u.created_at)


async def booking_rows(db: AsyncSession, bookings: Iterable[Booking]) -> list[BookingRow]:
    """Rows with business and staff names, in two batched queries."""
    bookings = list(bookings)
    business_ids = {b.business_id for b in bookings}
    staff_ids = {b.staff_id for b in bookings if b.staff_id}
    names = dict((await db.execute(select(Business.id, Business.name).where(Business.id.in_(business_ids)))).all()) if business_ids else {}
    staff = dict((await db.execute(select(Staff.id, Staff.name).where(Staff.id.in_(staff_ids)))).all()) if staff_ids else {}
    return [
        BookingRow(
            id=str(b.id), reference=b.reference, businessId=str(b.business_id), businessName=names.get(b.business_id, "—"),
            customerName=b.customer_name, customerPhone=b.customer_phone, staffName=staff.get(b.staff_id) if b.staff_id else None,
            status=_val(b.status),
            paymentStatus=_val(b.payment_status),
            source=_val(b.source),
            localDate=str(b.local_date), localTime=str(b.local_time)[:5],
            total=Money(amount=b.total_amount, currency=b.currency), createdAt=b.created_at,
        )
        for b in bookings
    ]


def destination_label(kind: Optional[str], party_b: Optional[str], account: Optional[str] = None) -> Optional[str]:
    if not kind:
        return None
    label = {"mpesa_till": "Till", "mpesa_paybill": "Paybill", "bank_account": "Bank paybill"}.get(kind, kind)
    # A till has no account number (the reference sent with it is the booking's).
    show_account = account and kind != "mpesa_till"
    return f"{label} {party_b or ''}{f' · acc {account}' if show_account else ''}".strip()


def payment_row(p: Payment) -> PaymentRow:
    return PaymentRow(
        id=str(p.id), bookingId=str(p.booking_id), visitId=str(p.visit_id) if p.visit_id else None,
        kind=_val(p.kind),
        provider=_val(p.provider),
        status=_val(p.status),
        amount=Money(amount=p.amount, currency=p.currency), tip=Money(amount=p.tip_amount or 0, currency=p.currency),
        phone=p.phone, failureReason=p.failure_reason, mpesaReceipt=p.mpesa_receipt,
        destination=destination_label(p.destination_type, p.party_b, p.account_reference),
        createdAt=p.created_at, settledAt=p.settled_at,
    )


# ─── Overview ────────────────────────────────────────────────


@router.get("/overview", response_model=OverviewResponse)
async def overview(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Platform numbers. Days are Nairobi days; cached for a minute (it's a dozen aggregates)."""
    return await cache.remember(f"{cache.PREFIX}admin:overview", OVERVIEW_TTL, lambda: _overview(db))


async def _overview(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    today, d7, d30 = _local_midnight(0), _local_midnight(6), _local_midnight(SERIES_DAYS - 1)

    def count_since(col, since):
        return func.count(case((col >= since, 1)))

    users = (await db.execute(select(
        func.count(User.id), count_since(User.created_at, today), count_since(User.created_at, d7),
        count_since(User.created_at, d30), func.count(User.disabled_at),
    ))).one()
    businesses = (await db.execute(select(
        func.count(case((Business.status == BusinessStatus.PUBLISHED, 1))),
        func.count(case((Business.status == BusinessStatus.DRAFT, 1))),
        func.count(case((and_(Business.status == BusinessStatus.PUBLISHED, Business.published_at >= d30), 1))),
    ))).one()
    bookings = (await db.execute(select(
        count_since(Booking.created_at, today), count_since(Booking.created_at, d7), count_since(Booking.created_at, d30),
        func.count(case((and_(Booking.created_at >= d30, Booking.source == "online"), 1))),
        func.count(case((and_(Booking.created_at >= d30, Booking.status == BookingStatus.CANCELLED), 1))),
        func.count(case((and_(Booking.created_at >= d30, Booking.status == BookingStatus.NO_SHOW), 1))),
    ))).one()
    paid = Payment.status == PaymentStatus.SUCCEEDED
    money = Payment.amount + func.coalesce(Payment.tip_amount, 0)
    payments = (await db.execute(select(
        func.coalesce(func.sum(case((and_(paid, Payment.settled_at >= today), money))), 0),
        func.coalesce(func.sum(case((and_(paid, Payment.settled_at >= d7), money))), 0),
        func.coalesce(func.sum(case((and_(paid, Payment.settled_at >= d30), money))), 0),
        func.count(case((and_(Payment.provider == "mpesa", Payment.created_at >= d7, Payment.status != PaymentStatus.PENDING), 1))),
        func.count(case((and_(Payment.provider == "mpesa", Payment.created_at >= d7, paid), 1))),
        func.count(case((Payment.status == PaymentStatus.PENDING, 1))),
    ))).one()
    refunds = (await db.execute(
        select(func.count(Booking.id), func.coalesce(func.sum(Booking.refund_amount), 0)).where(Booking.refund_status == "due")
    )).one()
    failures = (await db.execute(
        select(Payment.failure_reason, func.count(Payment.id))
        .where(Payment.provider == "mpesa", Payment.created_at >= d7,
               Payment.status.in_([PaymentStatus.FAILED, PaymentStatus.TIMEOUT]))
        .group_by(Payment.failure_reason).order_by(func.count(Payment.id).desc()).limit(5)
    )).all()

    series = {}
    for day in range(SERIES_DAYS - 1, -1, -1):
        key = (datetime.now(TZ).date() - timedelta(days=day)).isoformat()
        series[key] = {"date": key, "signups": 0, "bookings": 0, "volume": 0}
    for field, col, query in (
        ("signups", User.created_at, select(_local_day(db, User.created_at), func.count(User.id)).where(User.created_at >= d30)),
        ("bookings", Booking.created_at, select(_local_day(db, Booking.created_at), func.count(Booking.id)).where(Booking.created_at >= d30)),
        ("volume", Payment.settled_at, select(_local_day(db, Payment.settled_at), func.sum(money)).where(paid, Payment.settled_at >= d30)),
    ):
        for day, value in (await db.execute(query.group_by(_local_day(db, col)))).all():
            if day in series:
                series[day][field] = int(value or 0)

    attempts, succeeded = payments[3], payments[4]
    return OverviewResponse(
        asOf=now,
        users={"total": users[0], "today": users[1], "last7d": users[2], "last30d": users[3], "disabled": users[4]},
        businesses={"published": businesses[0], "drafts": businesses[1], "publishedLast30d": businesses[2]},
        bookings={"today": bookings[0], "last7d": bookings[1], "last30d": bookings[2], "onlineLast30d": bookings[3],
                  "cancelledLast30d": bookings[4], "noShowLast30d": bookings[5]},
        payments={"volumeToday": int(payments[0]), "volume7d": int(payments[1]), "volume30d": int(payments[2]),
                  "mpesaAttempts7d": attempts, "mpesaSucceeded7d": succeeded,
                  "successRate7d": round(succeeded * 100 / attempts) if attempts else None, "pending": payments[5]},
        refundsDue={"count": refunds[0], "amount": int(refunds[1])},
        topFailures=[FailureCount(reason=r or "unknown", count=c) for r, c in failures],
        series=[DayPoint(**v) for v in series.values()],
    ).model_dump(mode="json")


# ─── Users ───────────────────────────────────────────────────


@router.get("/users", response_model=UserPage)
async def list_users(
    q: Optional[str] = None,
    filter: str = Query("all", pattern="^(all|disabled|admins)$"),
    cursor: Optional[str] = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Newest first. q matches name, email or phone (any format: 0712…, +254712…)."""
    query = select(User)
    if q and q.strip():
        text = q.strip().lower()
        terms = [func.lower(User.email).contains(text), func.lower(User.first_name).contains(text),
                 func.lower(User.last_name).contains(text)]
        digits = _digits(text)
        if len(digits) >= 4:
            terms.append(User.phone.contains(digits))
        try:
            terms.append(User.id == uuid_pkg.UUID(text))
        except ValueError:
            pass
        query = query.where(or_(*terms))
    if filter == "disabled":
        query = query.where(User.disabled_at.is_not(None))
    elif filter == "admins":
        query = query.where(User.platform_role == "admin")
    if (after := _after(cursor, User.created_at, User.id)) is not None:
        query = query.where(after)
    rows = (await db.execute(query.order_by(User.created_at.desc(), User.id.desc()).limit(PAGE + 1))).scalars().all()
    page = rows[:PAGE]
    return UserPage(users=[user_row(u) for u in page],
                    nextCursor=_cursor(page[-1].created_at, page[-1].id) if len(rows) > PAGE else None)


@router.get("/users/{user_id}", response_model=UserDetail)
async def user_detail(user_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, _uuid(user_id, "user"))
    if user is None:
        raise _error(404, "not_found", "No user with that id")
    now = datetime.now(timezone.utc)
    sessions = (await db.execute(
        select(func.count(RefreshToken.id)).where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False),
                                                  RefreshToken.expires_at > now)
    )).scalar_one()

    owned = (await db.execute(select(Business).where(Business.owner_id == user.id))).scalars().all()
    staffed = (await db.execute(
        select(Staff, Business).join(Business, Business.id == Staff.business_id).where(Staff.user_id == user.id)
    )).all()
    workplaces = [Workplace(businessId=str(b.id), businessName=b.name, businessStatus=_val(b.status),
                            role="owner") for b in owned]
    owned_ids = {b.id for b in owned}
    workplaces += [
        Workplace(businessId=str(b.id), businessName=b.name, businessStatus=_val(b.status),
                  role=s.role, frontDeskAccess=bool(s.front_desk_access))
        for s, b in staffed if b.id not in owned_ids
    ]

    stats = (await db.execute(select(
        func.count(Booking.id),
        func.count(case((Booking.status == BookingStatus.COMPLETED, 1))),
        func.count(case((Booking.status == BookingStatus.NO_SHOW, 1))),
        func.count(case((Booking.status == BookingStatus.CANCELLED, 1))),
        func.coalesce(func.sum(case((Booking.payment_status == "paid", Booking.total_amount))), 0),
    ).where(Booking.customer_id == user.id))).one()
    bookings = (await db.execute(
        select(Booking).where(Booking.customer_id == user.id).order_by(Booking.created_at.desc()).limit(20)
    )).scalars().all()

    return UserDetail(
        user=user_row(user), disabledReason=user.disabled_reason, hasPassword=bool(user.password_hash),
        googleLinked=bool(user.google_sub), phoneVerifiedAt=user.phone_verified_at, twoStepSince=user.totp_enabled_at,
        activeSessions=sessions, workplaces=workplaces,
        bookingStats={"total": stats[0], "completed": stats[1], "noShows": stats[2], "cancelled": stats[3], "spent": int(stats[4])},
        bookings=await booking_rows(db, bookings), audit=await _audit_for(db, "user", user.id),
    )


# ─── Businesses ──────────────────────────────────────────────


async def business_rows(db: AsyncSession, businesses: list[Business]) -> list[BusinessRow]:
    ids = [b.id for b in businesses]
    if not ids:
        return []
    owners = {u.id: u for u in (await db.execute(select(User).where(User.id.in_({b.owner_id for b in businesses})))).scalars()}
    ratings = {bid: (n, float(avg or 0)) for bid, n, avg in (await db.execute(
        select(Review.business_id, func.count(Review.id), func.avg(Review.rating)).where(Review.business_id.in_(ids)).group_by(Review.business_id)
    )).all()}
    destinations = {bid: (t, v) for bid, t, v in (await db.execute(
        select(PaymentDestination.business_id, PaymentDestination.type, PaymentDestination.verification_status)
        .where(PaymentDestination.business_id.in_(ids))
    )).all()}
    rows = []
    for b in businesses:
        owner = owners.get(b.owner_id)
        count, avg = ratings.get(b.id, (0, 0.0))
        kind, check = destinations.get(b.id, (None, None))
        rows.append(BusinessRow(
            id=str(b.id), name=b.name, status=_val(b.status),
            categories=b.categories or [], phone=b.phone, address=(b.location or {}).get("address", ""),
            ownerId=str(b.owner_id), ownerName=_name(owner.first_name, owner.last_name, owner.email) if owner else "—",
            ownerEmail=owner.email if owner else None, ownerPhone=owner.phone if owner else None,
            rating=round(avg, 1) if count else 0.0, reviewCount=count,
            destinationType=_val(kind),
            destinationStatus=_val(check),
            createdAt=b.created_at, publishedAt=b.published_at,
            suspendedAt=b.suspended_at, suspendedReason=b.suspended_reason,
        ))
    return rows


@router.get("/businesses", response_model=BusinessPage)
async def list_businesses(
    q: Optional[str] = None,
    status: str = Query("all", pattern="^(all|published|draft)$"),
    cursor: Optional[str] = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Newest first. q matches the business name or phone, or the owner's name, email or phone."""
    query = select(Business)
    if q and q.strip():
        text = q.strip().lower()
        owner_match = select(User.id).where(or_(
            func.lower(User.email).contains(text), func.lower(User.first_name).contains(text), func.lower(User.last_name).contains(text),
        ))
        terms = [func.lower(Business.name).contains(text), Business.owner_id.in_(owner_match)]
        digits = _digits(text)
        if len(digits) >= 4:
            terms += [Business.phone.contains(digits), Business.owner_id.in_(select(User.id).where(User.phone.contains(digits)))]
        try:
            terms.append(Business.id == uuid_pkg.UUID(text))
        except ValueError:
            pass
        query = query.where(or_(*terms))
    if status != "all":
        query = query.where(Business.status == status)
    if (after := _after(cursor, Business.created_at, Business.id)) is not None:
        query = query.where(after)
    rows = (await db.execute(query.order_by(Business.created_at.desc(), Business.id.desc()).limit(PAGE + 1))).scalars().all()
    page = list(rows[:PAGE])
    return BusinessPage(businesses=await business_rows(db, page),
                        nextCursor=_cursor(page[-1].created_at, page[-1].id) if len(rows) > PAGE else None)


@router.get("/businesses/{business_id}", response_model=BusinessDetail)
async def business_detail(business_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    business = await db.get(Business, _uuid(business_id, "business"))
    if business is None:
        raise _error(404, "not_found", "No business with that id")
    since = _local_midnight(29)

    staff = (await db.execute(select(Staff).where(Staff.business_id == business.id).order_by(Staff.created_at))).scalars().all()
    people = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_({s.user_id for s in staff if s.user_id}))
    )).scalars()} if any(s.user_id for s in staff) else {}
    services = (await db.execute(select(func.count(Service.id)).where(Service.business_id == business.id))).scalar_one()
    hours = (await db.execute(select(WorkingHours.weekly_hours).where(WorkingHours.business_id == business.id))).scalar_one_or_none()
    policies = (await db.execute(select(BusinessPolicies).where(BusinessPolicies.business_id == business.id))).scalar_one_or_none()
    destination = (await db.execute(select(PaymentDestination).where(PaymentDestination.business_id == business.id))).scalar_one_or_none()

    stats = (await db.execute(select(
        func.count(Booking.id),
        func.count(case((Booking.source == "online", 1))),
        func.count(case((Booking.status == BookingStatus.COMPLETED, 1))),
        func.count(case((Booking.status == BookingStatus.CANCELLED, 1))),
        func.count(case((Booking.status == BookingStatus.NO_SHOW, 1))),
    ).where(Booking.business_id == business.id, Booking.created_at >= since))).one()
    volume = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount + func.coalesce(Payment.tip_amount, 0)), 0))
        .join(Booking, Booking.id == Payment.booking_id)
        .where(Booking.business_id == business.id, Payment.status == PaymentStatus.SUCCEEDED, Payment.settled_at >= since)
    )).scalar_one()
    bookings = (await db.execute(
        select(Booking).where(Booking.business_id == business.id).order_by(Booking.created_at.desc()).limit(20)
    )).scalars().all()
    payments = (await db.execute(
        select(Payment).join(Booking, Booking.id == Payment.booking_id)
        .where(Booking.business_id == business.id).order_by(Payment.created_at.desc()).limit(20)
    )).scalars().all()

    destination_info = None
    if destination is not None:
        kind = _val(destination.type)
        number = destination.till_number or destination.paybill_number or destination.bank_shortcode
        account = destination.paybill_account_number or destination.bank_account_number
        destination_info = {
            "type": kind, "label": destination_label(kind, number, account), "bankName": destination.bank_name,
            "verificationStatus": _val(destination.verification_status),
            "since": destination.created_at.isoformat() if destination.created_at else None,
            "reviewedAt": destination.reviewed_at.isoformat() if destination.reviewed_at else None,
        }

    return BusinessDetail(
        business=(await business_rows(db, [business]))[0],
        description=business.description or "", timezone=business.timezone or TZ_NAME,
        teamMode=_val(business.team_mode),
        servicesCount=services, workingHours=hours,
        policies={
            "depositRequired": bool(policies.deposit_required), "depositType": policies.deposit_type,
            "depositAmount": policies.deposit_amount, "depositPercent": policies.deposit_percent,
            "freeCancellationHours": policies.cancellation_free_hours,
            "lateCancellationFeePercent": policies.cancellation_late_fee_percent, "noShowFeePercent": policies.no_show_fee_percent,
        } if policies else {},
        destination=destination_info,
        team=[TeamMember(
            staffId=str(s.id), userId=str(s.user_id) if s.user_id else None, name=s.name, role=s.role,
            frontDeskAccess=bool(s.front_desk_access), commissionPercent=s.commission_percent,
            email=people[s.user_id].email if s.user_id in people else None,
            phone=people[s.user_id].phone if s.user_id in people else None,
        ) for s in staff],
        stats30d={"bookings": stats[0], "online": stats[1], "completed": stats[2], "cancelled": stats[3],
                  "noShows": stats[4], "volume": int(volume)},
        bookings=await booking_rows(db, bookings), payments=[payment_row(p) for p in payments],
        audit=await _audit_for(db, "business", business.id),
    )


# ─── Bookings & payments ─────────────────────────────────────


@router.get("/bookings", response_model=BookingLookup)
async def find_bookings(
    q: str = Query(..., min_length=3, description="Booking reference, M-Pesa receipt, customer phone or name, or a booking id"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    text = q.strip()
    limit = 50
    newest = (Booking.created_at.desc(),)

    try:
        found = await db.get(Booking, uuid_pkg.UUID(text))
        if found:
            return BookingLookup(bookings=await booking_rows(db, [found]), matchedBy="id")
    except ValueError:
        pass
    upper = text.upper().replace(" ", "")
    by_ref = (await db.execute(select(Booking).where(Booking.reference == upper))).scalars().all()
    if by_ref:
        return BookingLookup(bookings=await booking_rows(db, by_ref), matchedBy="reference")
    by_receipt = (await db.execute(
        select(Booking).join(Payment, Payment.booking_id == Booking.id).where(Payment.mpesa_receipt == upper)
    )).scalars().unique().all()
    if by_receipt:
        return BookingLookup(bookings=await booking_rows(db, by_receipt), matchedBy="receipt")
    digits = _digits(text)
    if len(digits) >= 6:
        by_phone = (await db.execute(
            select(Booking).where(or_(Booking.customer_phone.contains(digits),
                                      Booking.id.in_(select(Payment.booking_id).where(Payment.phone.contains(digits)))))
            .order_by(*newest).limit(limit)
        )).scalars().all()
        return BookingLookup(bookings=await booking_rows(db, by_phone), matchedBy="phone")
    by_name = (await db.execute(
        select(Booking).where(func.lower(Booking.customer_name).contains(text.lower())).order_by(*newest).limit(limit)
    )).scalars().all()
    return BookingLookup(bookings=await booking_rows(db, by_name), matchedBy="name")


@router.get("/bookings/{booking_id}", response_model=BookingDetail)
async def booking_detail(booking_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    booking = await db.get(Booking, _uuid(booking_id, "booking"))
    if booking is None:
        raise _error(404, "not_found", "No booking with that id")
    lines = (await db.execute(
        select(BookingLine).where(BookingLine.booking_id == booking.id).order_by(BookingLine.position)
    )).scalars().all()
    events = (await db.execute(
        select(BookingEvent).where(BookingEvent.booking_id == booking.id).order_by(BookingEvent.at)
    )).scalars().all()
    # Its own payments, plus visit payments that cover it (one payment for several bookings).
    covering = select(PaymentAllocation.payment_id).where(PaymentAllocation.booking_id == booking.id)
    payments = (await db.execute(
        select(Payment).where(or_(Payment.booking_id == booking.id, Payment.id.in_(covering))).order_by(Payment.created_at)
    )).scalars().all()
    review = (await db.execute(select(Review).where(Review.booking_id == booking.id))).scalar_one_or_none()
    actors = {u.id: _name(u.first_name, u.last_name, u.email) for u in (await db.execute(
        select(User).where(User.id.in_({e.actor_id for e in events if e.actor_id}))
    )).scalars()} if any(e.actor_id for e in events) else {}

    return BookingDetail(
        booking=(await booking_rows(db, [booking]))[0],
        customerId=str(booking.customer_id) if booking.customer_id else None, customerEmail=booking.customer_email,
        staffId=str(booking.staff_id) if booking.staff_id else None, visitId=str(booking.visit_id) if booking.visit_id else None,
        startsAt=booking.starts_at, durationMinutes=booking.duration_minutes,
        services=[{"name": l.name, "quantity": l.quantity, "durationMinutes": l.duration_minutes, "price": l.price_amount} for l in lines],
        deposit=Money(amount=booking.deposit_amount or 0, currency=booking.currency),
        tip=Money(amount=booking.tip_amount or 0, currency=booking.currency),
        staffCommission=booking.staff_commission, cancelReason=booking.cancel_reason,
        fees={"cancellation": booking.cancellation_fee, "noShow": booking.no_show_fee,
              "noShowUncollected": booking.no_show_fee_uncollected},
        refund={"status": _val(booking.refund_status), "amount": booking.refund_amount,
                "refundedAt": booking.refunded_at.isoformat() if booking.refunded_at else None, "reference": booking.refund_reference},
        notes=booking.notes,
        events=[BookingEventRow(fromStatus=_val(e.from_status), toStatus=_val(e.to_status),
                                actorName=actors.get(e.actor_id, "Customer or system") if e.actor_id else "System",
                                reason=e.reason, at=e.at) for e in events],
        payments=[payment_row(p) for p in payments],
        review={"id": str(review.id), "rating": review.rating, "text": review.text, "authorName": review.author_name,
                "createdAt": review.created_at.isoformat(), "replyText": review.reply_text,
                "hiddenAt": review.hidden_at.isoformat() if review.hidden_at else None} if review else None,
        audit=await _audit_for(db, "booking", booking.id),
    )


@router.post("/payments/{payment_id}/recheck", response_model=PaymentRow)
async def recheck_payment(payment_id: str, data: ReasonRequest, request: Request, admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)):
    """Ask M-Pesa now about a payment that's still pending (STK Query), instead of waiting for the
    reconciliation job. The answer goes through the same code as the callback."""
    from app.modules.payments.mpesa import MpesaConfigError, MpesaError
    from app.modules.payments.results import apply_result
    from app.modules.payments.service import get_provider

    payment = await db.get(Payment, _uuid(payment_id, "payment"))
    if payment is None:
        raise _error(404, "not_found", "No payment with that id")
    if payment.status != PaymentStatus.PENDING:
        raise _error(409, "not_pending", "This payment already has a result")
    if not payment.checkout_request_id:
        raise _error(409, "no_checkout_id", "M-Pesa never accepted this prompt; the reconciliation job will close it")
    before = _val(payment.status)
    try:
        result = await get_provider().check_payment(payment.checkout_request_id)
    except (MpesaError, MpesaConfigError) as err:
        raise _error(502, "mpesa_unavailable", f"M-Pesa didn't answer: {getattr(err, 'message', err)}")
    pid = payment.id
    if result is not None and result.status != "pending":
        await apply_result(db, result, datetime.now(timezone.utc))
    payment = await db.get(Payment, pid)
    await db.refresh(payment)
    audit.record(db, actor=admin, action="payment.recheck", target_type="booking", target_id=payment.booking_id,
                 reason=data.reason, changes={"payment": str(pid), "status": [before, _val(payment.status)]},
                 ip=client_ip(request))
    await db.commit()
    return payment_row(payment)
