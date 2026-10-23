"""Trust & safety for Trimmy's team (/admin/safety/* and actions on businesses, reviews, users).

Reports on the same thing are one case. Actions take a reason, write the audit log, and tell the
person affected (businesses and warnings; a hidden review simply disappears). Signals are worked
out live from the data (cached a few minutes): a queue for people to look at, nothing automatic.
"""
import uuid as uuid_pkg
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, cache
from app.core.database import get_db
from app.core.dependencies import require_admin
from app.modules.admin.auth import client_ip
from app.modules.admin.lookup import _audit_for, booking_rows, business_rows, user_row
from app.modules.admin.schemas import ReasonRequest
from app.modules.auth.models import AuthType, User
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.reviews import refresh_staff_rating
from app.modules.business.models import Business, PaymentDestination, Review, VerificationStatus
from app.modules.customers.models import BusinessCustomer
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.safety.models import Report, ReportStatus

router = APIRouter(prefix="/admin", tags=["Admin"])

SIGNALS_TTL = 300
CASES_SCAN = 500


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _uuid(value: str, what: str) -> uuid_pkg.UUID:
    try:
        return uuid_pkg.UUID(str(value))
    except ValueError:
        raise _error(404, "not_found", f"No {what} with that id")


def _aware(at):
    return at.replace(tzinfo=timezone.utc) if at is not None and at.tzinfo is None else at


def _name(u: Optional[User]) -> str:
    if u is None:
        return "Someone"
    return " ".join(p for p in (u.first_name, u.last_name) if p) or u.email or u.phone or "Someone"


async def _notify(db: AsyncSession, user_ids, *, type: str, title: str, body: str, data: dict, key: str, sms: bool = False):
    from app.modules.notifications.dispatch import deliver

    await deliver(db, user_ids, type=type, title=title, body=body, data=data, event_key=key, sms=f"Trimmy: {body}" if sms else None)


async def linked_account(db: AsyncSession, customer: BusinessCustomer) -> Optional[User]:
    """The Trimmy account a business's customer record belongs to: a verified phone first, then a
    verified email (the same rule the customers list uses for photos)."""
    if customer.phone:
        user = (await db.execute(select(User).where(User.phone == customer.phone, User.phone_verified_at.is_not(None)))).scalars().first()
        if user:
            return user
    if customer.email:
        return (await db.execute(select(User).where(
            func.lower(User.email) == customer.email.strip().lower(), User.verified.is_(True),
            User.auth_type.in_([AuthType.EMAIL, AuthType.GOOGLE]),
        ))).scalars().first()
    return None


# ─── Cases ───────────────────────────────────────────────────


class CaseRow(BaseModel):
    targetType: str
    targetId: str
    label: str
    sublabel: str
    businessId: Optional[str]
    reportCount: int
    reasons: dict[str, int]
    latestAt: datetime
    latestDetails: str
    actioned: bool = False  # the review is hidden / the business is suspended


class CasesResponse(BaseModel):
    cases: list[CaseRow]


async def _labels(db: AsyncSession, keys: set[tuple[str, str]]) -> dict:
    """(type, id) → (label, sublabel, actioned) in a few batched queries."""
    by_type = defaultdict(set)
    for t, i in keys:
        try:
            by_type[t].add(uuid_pkg.UUID(i))
        except ValueError:
            pass
    out = {}
    names = {}
    if by_type["business"]:
        for b in (await db.execute(select(Business).where(Business.id.in_(by_type["business"])))).scalars():
            out[("business", str(b.id))] = (b.name, (b.location or {}).get("address", ""), b.suspended_at is not None)
    if by_type["review"]:
        reviews = (await db.execute(select(Review).where(Review.id.in_(by_type["review"])))).scalars().all()
        names = dict((await db.execute(select(Business.id, Business.name).where(Business.id.in_({r.business_id for r in reviews})))).all()) if reviews else {}
        for r in reviews:
            out[("review", str(r.id))] = (f"{'★' * r.rating} review by {r.author_name}", f"{names.get(r.business_id, '')} · “{(r.text or '')[:60]}”", r.hidden_at is not None)
    if by_type["customer"]:
        customers = (await db.execute(select(BusinessCustomer).where(BusinessCustomer.id.in_(by_type["customer"])))).scalars().all()
        names = dict((await db.execute(select(Business.id, Business.name).where(Business.id.in_({c.business_id for c in customers})))).all()) if customers else {}
        for c in customers:
            out[("customer", str(c.id))] = (c.name, f"Customer of {names.get(c.business_id, '')}{f' · {c.phone}' if c.phone else ''}", False)
    if by_type["booking"]:
        bookings = (await db.execute(select(Booking).where(Booking.id.in_(by_type["booking"])))).scalars().all()
        names = dict((await db.execute(select(Business.id, Business.name).where(Business.id.in_({b.business_id for b in bookings})))).all()) if bookings else {}
        for b in bookings:
            out[("booking", str(b.id))] = (f"Booking {b.reference}", f"{b.customer_name} at {names.get(b.business_id, '')}", False)
    return out


@router.get("/safety/cases", response_model=CasesResponse)
async def list_cases(
    status: Literal["open", "actioned", "dismissed"] = "open",
    targetType: Optional[Literal["business", "review", "customer", "booking"]] = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Reports grouped by what they're about; most reported first, then most recent."""
    query = select(Report).where(Report.status == status)
    if targetType:
        query = query.where(Report.target_type == targetType)
    reports = (await db.execute(query.order_by(Report.created_at.desc()).limit(CASES_SCAN))).scalars().all()
    groups: dict = {}
    for r in reports:
        groups.setdefault((r.target_type, r.target_id), []).append(r)
    labels = await _labels(db, set(groups))
    cases = []
    for (t, i), rows in groups.items():
        label, sub, actioned = labels.get((t, i), ("(deleted)", "", False))
        reasons: dict = defaultdict(int)
        for r in rows:
            reasons[r.reason] += 1
        cases.append(CaseRow(
            targetType=t, targetId=i, label=label, sublabel=sub, businessId=str(rows[0].business_id) if rows[0].business_id else None,
            reportCount=len(rows), reasons=dict(reasons), latestAt=_aware(rows[0].created_at), latestDetails=rows[0].details[:200],
            actioned=actioned,
        ))
    cases.sort(key=lambda c: (-c.reportCount, -c.latestAt.timestamp()))
    return CasesResponse(cases=cases)


class ReportOut(BaseModel):
    id: str
    reporterId: Optional[str]
    reporterName: str
    reason: str
    details: str
    status: str
    createdAt: datetime
    resolution: Optional[str]


class CaseDetail(BaseModel):
    targetType: str
    targetId: str
    label: str
    sublabel: str
    target: dict[str, Any]  # what it is, shaped per type (see _target)
    reports: list[ReportOut]
    audit: list[Any]


async def _target(db: AsyncSession, t: str, tid: uuid_pkg.UUID) -> dict:
    if t == "business":
        b = await db.get(Business, tid)
        if b is None:
            return {}
        row = (await business_rows(db, [b]))[0].model_dump(mode="json")
        return {"business": row, "suspendedAt": b.suspended_at.isoformat() if b.suspended_at else None,
                "suspendedReason": b.suspended_reason, "ownerId": str(b.owner_id)}
    if t == "review":
        r = await db.get(Review, tid)
        if r is None:
            return {}
        b = await db.get(Business, r.business_id)
        return {"review": {"id": str(r.id), "rating": r.rating, "text": r.text, "authorName": r.author_name,
                           "customerId": str(r.customer_id) if r.customer_id else None, "bookingId": str(r.booking_id) if r.booking_id else None,
                           "createdAt": _aware(r.created_at).isoformat(), "replyText": r.reply_text,
                           "hiddenAt": _aware(r.hidden_at).isoformat() if r.hidden_at else None, "hiddenReason": r.hidden_reason},
                "business": (await business_rows(db, [b]))[0].model_dump(mode="json") if b else None}
    if t == "customer":
        c = await db.get(BusinessCustomer, tid)
        if c is None:
            return {}
        b = await db.get(Business, c.business_id)
        account = await linked_account(db, c)
        no_shows = (await db.execute(select(func.count(Booking.id)).where(
            Booking.business_id == c.business_id, Booking.customer_phone == c.phone, Booking.status == BookingStatus.NO_SHOW,
        ))).scalar_one() if c.phone else 0
        return {"customer": {"id": str(c.id), "name": c.name, "phone": c.phone, "email": c.email, "noShowsHere": no_shows},
                "business": (await business_rows(db, [b]))[0].model_dump(mode="json") if b else None,
                "account": user_row(account).model_dump(mode="json") if account else None}
    booking = await db.get(Booking, tid)
    return {"booking": (await booking_rows(db, [booking]))[0].model_dump(mode="json")} if booking else {}


@router.get("/safety/cases/{target_type}/{target_id}", response_model=CaseDetail)
async def case_detail(target_type: Literal["business", "review", "customer", "booking"], target_id: str,
                      admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    tid = _uuid(target_id, target_type)
    reports = (await db.execute(
        select(Report).where(Report.target_type == target_type, Report.target_id == str(tid)).order_by(Report.created_at.desc())
    )).scalars().all()
    reporters = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_({r.reporter_id for r in reports if r.reporter_id}))
    )).scalars()} if reports else {}
    label, sub, _ = (await _labels(db, {(target_type, str(tid))})).get((target_type, str(tid)), ("(deleted)", "", False))
    audit_type = {"customer": "customer"}.get(target_type, target_type)
    return CaseDetail(
        targetType=target_type, targetId=str(tid), label=label, sublabel=sub, target=await _target(db, target_type, tid),
        reports=[ReportOut(id=str(r.id), reporterId=str(r.reporter_id) if r.reporter_id else None,
                           reporterName=_name(reporters.get(r.reporter_id)), reason=r.reason, details=r.details, status=r.status,
                           createdAt=_aware(r.created_at), resolution=r.resolution) for r in reports],
        audit=await _audit_for(db, audit_type, tid),
    )


class ResolveRequest(ReasonRequest):
    outcome: Literal["actioned", "dismissed"]


@router.post("/safety/cases/{target_type}/{target_id}/resolve", response_model=CaseDetail)
async def resolve_case(target_type: Literal["business", "review", "customer", "booking"], target_id: str, data: ResolveRequest,
                       request: Request, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Close every open report on this thing (actioned: something was done; dismissed: nothing to do)."""
    tid = _uuid(target_id, target_type)
    open_reports = (await db.execute(
        select(Report).where(Report.target_type == target_type, Report.target_id == str(tid), Report.status == ReportStatus.OPEN)
    )).scalars().all()
    now = datetime.now(timezone.utc)
    for r in open_reports:
        r.status, r.resolved_at, r.resolved_by, r.resolution = data.outcome, now, admin.id, data.reason
    audit.record(db, actor=admin, action=f"report.{'action' if data.outcome == 'actioned' else 'dismiss'}", target_type=target_type,
                 target_id=tid, reason=data.reason, changes={"reports": len(open_reports)}, ip=client_ip(request))
    await db.commit()
    return await case_detail(target_type, target_id, admin, db)


# ─── Actions ─────────────────────────────────────────────────


@router.post("/reviews/{review_id}/hide")
async def hide_review(review_id: str, data: ReasonRequest, request: Request, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    """Off the business's profile, its review lists and every rating, at once (the cache clears on commit)."""
    review = await db.get(Review, _uuid(review_id, "review"))
    if review is None:
        raise _error(404, "not_found", "No review with that id")
    if review.hidden_at is None:
        review.hidden_at, review.hidden_reason = datetime.now(timezone.utc), data.reason
        await db.flush()
        if review.staff_id:
            await refresh_staff_rating(db, review.staff_id)
        audit.record(db, actor=admin, action="review.hide", target_type="review", target_id=review.id, reason=data.reason, ip=client_ip(request))
        await db.commit()
    return {"id": str(review.id), "hiddenAt": review.hidden_at}


@router.post("/reviews/{review_id}/restore")
async def restore_review(review_id: str, data: ReasonRequest, request: Request, admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)):
    review = await db.get(Review, _uuid(review_id, "review"))
    if review is None:
        raise _error(404, "not_found", "No review with that id")
    if review.hidden_at is not None:
        review.hidden_at = review.hidden_reason = None
        await db.flush()
        if review.staff_id:
            await refresh_staff_rating(db, review.staff_id)
        audit.record(db, actor=admin, action="review.restore", target_type="review", target_id=review.id, reason=data.reason, ip=client_ip(request))
        await db.commit()
    return {"id": str(review.id), "hiddenAt": None}


class SuspendRequest(ReasonRequest):
    # Shown to the owner (the reason is for the audit log). Defaults to a general message.
    messageToOwner: Optional[str] = Field(None, max_length=500)


@router.post("/businesses/{business_id}/suspend")
async def suspend_business(business_id: str, data: SuspendRequest, request: Request, admin: User = Depends(require_admin),
                           db: AsyncSession = Depends(get_db)):
    """Hidden from customers and no new online bookings. The owner's calendar, walk-ins, checkout and
    existing bookings keep working (customers are never stranded). The owner is told, with a way to reply."""
    business = await db.get(Business, _uuid(business_id, "business"))
    if business is None:
        raise _error(404, "not_found", "No business with that id")
    if business.suspended_at is None:
        message = (data.messageToOwner or "").strip() or "Your business is paused on Trimmy while our team looks into a report. Reply from Help to talk to us."
        business.suspended_at, business.suspended_reason = datetime.now(timezone.utc), message
        audit.record(db, actor=admin, action="business.suspend", target_type="business", target_id=business.id,
                     reason=data.reason, changes={"messageToOwner": message}, ip=client_ip(request))
        await db.commit()
        await _notify(db, [business.owner_id], type="business_suspended", title=f"{business.name} is paused on Trimmy", body=message,
                      data={"businessId": str(business.id)}, key=f"business_suspended:{business.id}:{business.suspended_at.isoformat()}", sms=True)
    return {"id": str(business.id), "suspendedAt": business.suspended_at, "suspendedReason": business.suspended_reason}


@router.post("/businesses/{business_id}/unsuspend")
async def unsuspend_business(business_id: str, data: ReasonRequest, request: Request, admin: User = Depends(require_admin),
                             db: AsyncSession = Depends(get_db)):
    business = await db.get(Business, _uuid(business_id, "business"))
    if business is None:
        raise _error(404, "not_found", "No business with that id")
    if business.suspended_at is not None:
        business.suspended_at = business.suspended_reason = None
        audit.record(db, actor=admin, action="business.unsuspend", target_type="business", target_id=business.id,
                     reason=data.reason, ip=client_ip(request))
        await db.commit()
        await _notify(db, [business.owner_id], type="business_restored", title=f"{business.name} is back on Trimmy",
                      body="Customers can find and book you again.", data={"businessId": str(business.id)},
                      key=f"business_restored:{business.id}:{datetime.now(timezone.utc).isoformat()}")
    return {"id": str(business.id), "suspendedAt": None, "suspendedReason": None}


class WarnRequest(ReasonRequest):
    message: str = Field(min_length=5, max_length=1000)


@router.post("/users/{user_id}/warn")
async def warn_user(user_id: str, data: WarnRequest, request: Request, admin: User = Depends(require_admin),
                    db: AsyncSession = Depends(get_db)):
    """A message from Trimmy's team, in their inbox and as a push / SMS."""
    user = await db.get(User, _uuid(user_id, "user"))
    if user is None:
        raise _error(404, "not_found", "No user with that id")
    audit.record(db, actor=admin, action="user.warn", target_type="user", target_id=user.id, reason=data.reason,
                 changes={"message": data.message}, ip=client_ip(request))
    await db.commit()
    await _notify(db, [user.id], type="trimmy_notice", title="A message from Trimmy", body=data.message.strip(),
                  data={}, key=f"trimmy_notice:{user.id}:{datetime.now(timezone.utc).isoformat()}", sms=True)
    return {"ok": True}


# ─── Payment details ─────────────────────────────────────────


class DestinationRow(BaseModel):
    businessId: str
    businessName: str
    ownerName: str
    ownerPhone: Optional[str]
    type: str
    label: str
    bankName: Optional[str]
    verificationStatus: str
    setAt: Optional[datetime]
    reviewedAt: Optional[datetime]


@router.get("/safety/destinations", response_model=list[DestinationRow])
async def destinations(filter: Literal["unreviewed", "failed", "all"] = "unreviewed", admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)):
    """Where businesses get paid. Unreviewed: set or changed since someone on the team checked."""
    from app.modules.admin.lookup import destination_label

    query = select(PaymentDestination, Business, User).join(Business, Business.id == PaymentDestination.business_id).join(User, User.id == Business.owner_id)
    if filter == "unreviewed":
        query = query.where(PaymentDestination.reviewed_at.is_(None), PaymentDestination.verification_status != VerificationStatus.FAILED)
    elif filter == "failed":
        query = query.where(PaymentDestination.verification_status == VerificationStatus.FAILED)
    rows = (await db.execute(query.order_by(PaymentDestination.created_at.desc()).limit(200))).all()
    return [
        DestinationRow(
            businessId=str(b.id), businessName=b.name, ownerName=_name(owner), ownerPhone=owner.phone,
            type=str(d.type.value if hasattr(d.type, "value") else d.type),
            label=destination_label(str(d.type.value if hasattr(d.type, "value") else d.type),
                                    d.till_number or d.paybill_number or d.bank_shortcode,
                                    d.paybill_account_number or d.bank_account_number) or "",
            bankName=d.bank_name, verificationStatus=str(d.verification_status.value if hasattr(d.verification_status, "value") else d.verification_status),
            setAt=_aware(d.created_at), reviewedAt=_aware(d.reviewed_at),
        )
        for d, b, owner in rows
    ]


class DestinationReview(ReasonRequest):
    decision: Literal["ok", "failed"]


@router.post("/businesses/{business_id}/destination/review")
async def review_destination(business_id: str, data: DestinationReview, request: Request, admin: User = Depends(require_admin),
                             db: AsyncSession = Depends(get_db)):
    """ok: checked (e.g. the till name matches the business). failed: M-Pesa prompts stop until the
    owner enters new details (which come back here to check); the owner is told why."""
    business = await db.get(Business, _uuid(business_id, "business"))
    destination = (await db.execute(select(PaymentDestination).where(PaymentDestination.business_id == business.id))).scalar_one_or_none() if business else None
    if destination is None:
        raise _error(404, "not_found", "That business has no payment details")
    before = str(destination.verification_status.value if hasattr(destination.verification_status, "value") else destination.verification_status)
    destination.reviewed_at, destination.reviewed_by = datetime.now(timezone.utc), admin.id
    destination.verification_status = VerificationStatus.VERIFIED if data.decision == "ok" else VerificationStatus.FAILED
    audit.record(db, actor=admin, action=f"destination.{data.decision}", target_type="business", target_id=business.id,
                 reason=data.reason, changes={"verificationStatus": [before, data.decision]}, ip=client_ip(request))
    await db.commit()
    if data.decision == "failed":
        await _notify(db, [business.owner_id], type="payment_details_failed", title="Check your payment details",
                      body=f"We couldn't confirm where {business.name} gets paid, so M-Pesa payments are paused. Update your payment details in settings.",
                      data={"businessId": str(business.id)}, key=f"payment_details_failed:{business.id}:{destination.reviewed_at.isoformat()}", sms=True)
    return {"businessId": str(business.id), "verificationStatus": destination.verification_status, "reviewedAt": destination.reviewed_at}


# ─── Signals ─────────────────────────────────────────────────


@router.get("/safety/signals")
async def signals(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Things worth a look, worked out live (cached 5 minutes). Nothing here acts by itself."""
    return await cache.remember(f"{cache.PREFIX}admin:signals", SIGNALS_TTL, lambda: _signals(db))


async def _signals(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    d60, d7 = now - timedelta(days=60), now - timedelta(days=7)

    reported = (await db.execute(
        select(Report.target_type, Report.target_id, func.count(Report.id)).where(Report.status == ReportStatus.OPEN)
        .group_by(Report.target_type, Report.target_id).having(func.count(Report.id) >= 3).order_by(func.count(Report.id).desc()).limit(20)
    )).all()
    labels = await _labels(db, {(t, i) for t, i, _ in reported})

    no_shows = (await db.execute(
        select(Booking.customer_id, func.count(Booking.id)).where(
            Booking.customer_id.is_not(None), Booking.status == BookingStatus.NO_SHOW, Booking.starts_at >= d60,
        ).group_by(Booking.customer_id).having(func.count(Booking.id) >= 3).order_by(func.count(Booking.id).desc()).limit(20)
    )).all()
    people = {u.id: u for u in (await db.execute(select(User).where(User.id.in_([c for c, _ in no_shows])))).scalars()} if no_shows else {}

    low_rated = (await db.execute(
        select(Review.business_id, func.count(Review.id), func.avg(Review.rating)).where(
            Review.created_at >= d60, Review.hidden_at.is_(None),
        ).group_by(Review.business_id).having(and_(func.count(Review.id) >= 5, func.avg(Review.rating) < 3)).limit(20)
    )).all()

    stale = (await db.execute(
        select(Booking.business_id, func.count(Booking.id), func.coalesce(func.sum(Booking.refund_amount), 0)).where(
            Booking.refund_status == "due", Booking.updated_at <= d7,
        ).group_by(Booking.business_id).order_by(func.count(Booking.id).desc()).limit(20)
    )).all()

    attempts = (await db.execute(
        select(Booking.business_id, func.count(Payment.id), func.count(Payment.id).filter(Payment.status == PaymentStatus.SUCCEEDED))
        .join(Booking, Booking.id == Payment.booking_id)
        .where(Payment.provider == "mpesa", Payment.created_at >= d7, Payment.status != PaymentStatus.PENDING)
        .group_by(Booking.business_id).having(func.count(Payment.id) >= 10)
    )).all()
    failing = [(bid, total, ok) for bid, total, ok in attempts if ok * 2 < total]

    business_ids = {b for b, *_ in low_rated} | {b for b, *_ in stale} | {b for b, *_ in failing}
    names = dict((await db.execute(select(Business.id, Business.name).where(Business.id.in_(business_ids))))) if business_ids else {}

    return {
        "asOf": now.isoformat(),
        "repeatReports": [{"targetType": t, "targetId": i, "count": n, "label": labels.get((t, i), ("(deleted)", "", False))[0]} for t, i, n in reported],
        "noShowCustomers": [{"userId": str(c), "name": _name(people.get(c)), "phone": people[c].phone if c in people else None, "count": n} for c, n in no_shows],
        "lowRatedBusinesses": [{"businessId": str(b), "name": names.get(b, ""), "reviews": n, "rating": round(float(avg), 1)} for b, n, avg in low_rated],
        "staleRefunds": [{"businessId": str(b), "name": names.get(b, ""), "count": n, "amount": int(amount)} for b, n, amount in stale],
        "failingPayments": [{"businessId": str(b), "name": names.get(b, ""), "attempts": total, "succeeded": ok} for b, total, ok in failing],
    }
