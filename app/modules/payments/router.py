import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_verified_user
from app.modules.auth.models import User
from app.modules.payments.mpesa import MpesaError, parse_callback
from app.modules.payments.results import apply_result
from app.modules.payments.schemas import ChargeRequest, StartPaymentRequest, StartPaymentResponse, VisitChargeRequest
from app.modules.payments.service import PaymentsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])
booking_payments_router = APIRouter(prefix="/bookings", tags=["Payments"])
visit_payments_router = APIRouter(prefix="/visits", tags=["Payments"])


@booking_payments_router.post(
    "/{booking_id}/payments", status_code=status.HTTP_202_ACCEPTED, response_model=StartPaymentResponse
)
async def start_booking_payment(
    booking_id: str,
    data: StartPaymentRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Send the M-Pesa prompt for a pending_payment booking's deposit, routed to the business's
    payment destination. Then poll GET /bookings/{id} until status is confirmed or
    depositPayment.status is failed/timeout."""
    return await PaymentsService(db).start_deposit(current_user, booking_id, data.phone)


@booking_payments_router.post(
    "/{booking_id}/charge",
    response_model=None,
    responses={
        200: {"description": "Cash, or nothing left to pay: the completed Booking"},
        202: {"description": "M-Pesa prompt sent; poll GET /bookings/{id}", "model": StartPaymentResponse},
    },
)
async def charge_booking(
    booking_id: str,
    data: ChargeRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner or front desk: take the balance (total minus any deposit paid) for an in-progress
    booking. Cash completes it at once; M-Pesa sends the prompt, then poll GET /bookings/{id}."""
    code, body = await PaymentsService(db).charge(current_user, booking_id, data.method, data.phone, data.tip)
    return JSONResponse(status_code=code, content=jsonable_encoder(body, by_alias=True))


@visit_payments_router.post(
    "/{visit_id}/charge",
    response_model=None,
    responses={
        200: {"description": "Cash, or nothing left to pay: the Visit, paid"},
        202: {"description": "M-Pesa prompt sent; poll GET /visits/{id}", "model": StartPaymentResponse},
    },
)
async def charge_visit(
    visit_id: str,
    data: VisitChargeRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner or front desk: check out a multi-staff visit in one payment (cash or one M-Pesa
    prompt): every part's balance, plus each person's share of the tip as the customer split it.
    409 visit_not_ready lists parts not started yet (start, cancel, or pass bookingIds)."""
    code, body = await PaymentsService(db).charge_visit(
        current_user, visit_id, data.method, data.phone, data.tips, data.booking_ids,
    )
    return JSONResponse(status_code=code, content=jsonable_encoder(body, by_alias=True))


@router.post("/stk/callback/{secret}", include_in_schema=False)
async def mpesa_callback(secret: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Daraja posts STK push results here (the URL comes from MPESA_CALLBACK_URL + secret).

    Records the result on the payment and confirms the booking (BK-32).
    Idempotent: Daraja retries until it gets the acknowledgement.
    """
    expected = settings.MPESA_CALLBACK_SECRET
    if not expected or not hmac.compare_digest(secret, expected):
        # Same answer as an unknown route, so the endpoint doesn't reveal itself.
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Not Found"})

    try:
        body = await request.json()
        result = parse_callback(body)
    except (ValueError, MpesaError) as e:
        logger.warning(f"Ignoring malformed M-Pesa callback: {e}")
        raise HTTPException(status_code=400, detail={"error": "invalid_callback", "message": "Unrecognised callback"})

    logger.info(
        "M-Pesa callback: checkout=%s status=%s reason=%s code=%s receipt=%s amount=%s desc=%r",
        result.checkout_request_id, result.status, result.failure_reason, result.result_code,
        result.receipt, result.amount, result.result_desc,
    )
    await apply_result(db, result, datetime.now(timezone.utc), raw=body)
    # Daraja retries until it gets this acknowledgement.
    return {"ResultCode": 0, "ResultDesc": "Accepted"}
