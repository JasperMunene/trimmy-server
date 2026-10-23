import uuid as uuid_pkg
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_verified_user
from app.modules.auth.models import User
from app.modules.earnings.schemas import EarningsSummaryResponse
from app.modules.earnings.service import EarningsService

router = APIRouter(prefix="/businesses", tags=["Earnings"])


@router.get("/{business_id}/earnings", response_model=EarningsSummaryResponse)
async def earnings_summary(
    business_id: str,
    start: date = Query(..., alias="from", description="First day, business local time"),
    end: date = Query(..., alias="to", description="Last day, inclusive; at most 366 days"),
    staff_id: Optional[uuid_pkg.UUID] = Query(None, alias="staffId", description="Scope every number to one staff member"),
    transactions_limit: Optional[int] = Query(None, alias="transactionsLimit", description="Newest N transactions; all if omitted"),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Revenue for a date range: owner (whole business or one staff member); staff (always
    themselves). Only paid bookings count toward money."""
    return await EarningsService(db).summary(current_user, business_id, start, end, staff_id, transactions_limit)
