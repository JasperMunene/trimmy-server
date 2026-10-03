from typing import Optional
import uuid as uuid_pkg

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_verified_user
from app.modules.auth.models import User
from app.modules.payouts.schemas import (
    AdjustmentResponse,
    CreateAdjustmentRequest,
    ListPayoutsResponse,
    PayoutBalanceResponse,
    PayoutResponse,
    RequestPayoutRequest,
    RespondToPayoutRequest,
    WalletResponse,
    WalletStatementResponse,
)
from app.modules.payouts.service import PayoutsService
from app.modules.payouts.wallet import STATEMENT_MAX_LIMIT, WalletService

router = APIRouter(tags=["Payouts"])


@router.post("/payouts", status_code=status.HTTP_201_CREATED, response_model=PayoutResponse)
async def request_payout(
    data: RequestPayoutRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Staff request a withdrawal from their wallet, up to their available balance. Always pending."""
    return await PayoutsService(db).request(current_user, data.business_id, data.staff_id, data.amount)


@router.get("/businesses/{business_id}/payouts", response_model=ListPayoutsResponse)
async def list_business_payouts(
    business_id: str,
    status_filter: Optional[str] = Query(None, alias="status", description="pending | paid | rejected"),
    staff_id: Optional[uuid_pkg.UUID] = Query(None, alias="staffId"),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Newest first. The owner sees everyone's; staff see only their own."""
    return await PayoutsService(db).list_for_business(current_user, business_id, status_filter, staff_id)


@router.post("/payouts/{payout_id}/respond", response_model=PayoutResponse)
async def respond_to_payout(
    payout_id: str,
    data: RespondToPayoutRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner: approve (records that you've paid them yourself) or reject (frees the amount again)."""
    return await PayoutsService(db).respond(current_user, payout_id, data.action, data.rejection_reason)


@router.get("/me/payouts/balance", response_model=PayoutBalanceResponse)
async def my_payout_balance(
    business_id: str = Query(..., alias="businessId"),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Staff: my wallet at this business (earned, pending, paid, available)."""
    return await PayoutsService(db).my_balance(current_user, business_id)


@router.get("/businesses/{business_id}/staff/{staff_id}/payout-balance", response_model=PayoutBalanceResponse)
async def staff_payout_balance(
    business_id: str,
    staff_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner (any staff member) or the staff member themselves."""
    return await PayoutsService(db).staff_balance(current_user, business_id, staff_id)


# ─── Wallet: across businesses, statement, adjustments ───────


@router.get("/me/wallet", response_model=WalletResponse)
async def my_wallet(current_user: User = Depends(get_current_verified_user), db: AsyncSession = Depends(get_db)):
    """Staff: my balance at every business I work at, and the totals across them."""
    return await WalletService(db).my_wallet(current_user)


@router.get("/me/wallet/transactions", response_model=WalletStatementResponse)
async def my_wallet_statement(
    business_id: Optional[str] = Query(None, alias="businessId", description="One business; omit for all of them"),
    limit: int = Query(50, ge=1, le=STATEMENT_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Staff: commission, tips, bonuses, deductions, advances and payouts, newest first, with the
    balance after each one."""
    return await WalletService(db).my_statement(current_user, business_id, limit, offset)


@router.get("/businesses/{business_id}/staff/{staff_id}/wallet/transactions", response_model=WalletStatementResponse)
async def staff_wallet_statement(
    business_id: str,
    staff_id: str,
    limit: int = Query(50, ge=1, le=STATEMENT_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner (any staff member) or the staff member themselves."""
    return await WalletService(db).staff_statement(current_user, business_id, staff_id, limit, offset)


@router.post(
    "/businesses/{business_id}/staff/{staff_id}/wallet-adjustments",
    status_code=status.HTTP_201_CREATED,
    response_model=AdjustmentResponse,
)
async def add_wallet_adjustment(
    business_id: str,
    staff_id: str,
    data: CreateAdjustmentRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner: tip, bonus, deduction or advance. Front desk: tips only."""
    return await WalletService(db).add_adjustment(current_user, business_id, staff_id, data)


@router.post("/wallet-adjustments/{adjustment_id}/void", response_model=AdjustmentResponse)
async def void_wallet_adjustment(
    adjustment_id: str,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner: undo a mistaken adjustment (kept on record, no longer counted)."""
    return await WalletService(db).void_adjustment(current_user, adjustment_id)
