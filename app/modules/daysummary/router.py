from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_verified_user
from app.modules.auth.models import User
from app.modules.daysummary.schemas import DaySummaryResponse, SubmitDayRequest
from app.modules.daysummary.service import DaySummaryService

router = APIRouter(prefix="/businesses", tags=["Day summary"])


@router.get("/{business_id}/day-summary", response_model=DaySummaryResponse)
async def get_day_summary(
    business_id: str,
    day: date = Query(..., alias="date", description="Business-local day"),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner or front desk: the day's visits, money collected, refunds paid and what's still unpaid (F4)."""
    return await DaySummaryService(db).summary(current_user, business_id, day)


@router.post("/{business_id}/day-summary/{day}/submit", response_model=DaySummaryResponse)
async def submit_day_summary(
    business_id: str,
    day: date,
    data: SubmitDayRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Front desk (or owner): sign the day off with an optional note. The owner is told by SMS and push.
    Resubmitting replaces the note."""
    return await DaySummaryService(db).submit(current_user, business_id, day, data.notes)
