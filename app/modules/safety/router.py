"""POST /reports: tell Trimmy's team about a business, a review, a customer or a booking.

Who can report what:
- a business or a review: anyone signed in (not their own business or their own review);
- a customer (a business's customer record): the owner or staff of that business;
- a booking: the customer who made it, or the business's owner or staff.
The same person reporting the same thing again (still open) updates their report instead.
"""
import uuid as uuid_pkg
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.rate_limit import limiter
from app.modules.auth.models import User
from app.modules.booking.models import Booking
from app.modules.business.models import Business, Review, Staff
from app.modules.customers.models import BusinessCustomer
from app.modules.safety.models import Report, ReportStatus

router = APIRouter(prefix="/reports", tags=["Safety"])


class ReportRequest(BaseModel):
    targetType: Literal["business", "review", "customer", "booking"]
    targetId: str
    reason: Literal["fraud", "harassment", "inappropriate", "fake_review", "safety", "no_show", "other"]
    details: Optional[str] = Field(None, max_length=2000)


class ReportResponse(BaseModel):
    reportId: str
    status: str


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "not_found", "message": f"No {what} with that id"})


async def _works_at(db: AsyncSession, user: User, business_id) -> bool:
    business = await db.get(Business, business_id)
    if business is not None and business.owner_id == user.id:
        return True
    return (await db.execute(select(Staff.id).where(Staff.business_id == business_id, Staff.user_id == user.id))).first() is not None


@router.post("", response_model=ReportResponse, status_code=201)
@limiter.limit("20/hour")
async def report(request: Request, data: ReportRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        tid = uuid_pkg.UUID(data.targetId)
    except ValueError:
        raise _not_found(data.targetType)

    business_id = None
    if data.targetType == "business":
        business = await db.get(Business, tid)
        if business is None:
            raise _not_found("business")
        if business.owner_id == user.id:
            raise HTTPException(status_code=400, detail={"error": "validation_error", "message": "You can't report your own business"})
        business_id = business.id
    elif data.targetType == "review":
        review = await db.get(Review, tid)
        if review is None:
            raise _not_found("review")
        if review.customer_id == user.id:
            raise HTTPException(status_code=400, detail={"error": "validation_error", "message": "You can't report your own review"})
        business_id = review.business_id
    elif data.targetType == "customer":
        customer = await db.get(BusinessCustomer, tid)
        if customer is None or not await _works_at(db, user, customer.business_id):
            raise _not_found("customer")
        business_id = customer.business_id
    else:
        booking = await db.get(Booking, tid)
        if booking is None or (booking.customer_id != user.id and not await _works_at(db, user, booking.business_id)):
            raise _not_found("booking")
        business_id = booking.business_id

    existing = (await db.execute(
        select(Report).where(Report.reporter_id == user.id, Report.target_type == data.targetType,
                             Report.target_id == str(tid), Report.status == ReportStatus.OPEN)
    )).scalar_one_or_none()
    if existing is not None:
        existing.reason = data.reason
        existing.details = (data.details or "").strip()
        report_row = existing
    else:
        report_row = Report(reporter_id=user.id, target_type=data.targetType, target_id=str(tid), business_id=business_id,
                            reason=data.reason, details=(data.details or "").strip())
        db.add(report_row)
    await db.commit()
    return ReportResponse(reportId=str(report_row.id), status=report_row.status)
