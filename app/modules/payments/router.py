import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_verified_user
from app.modules.auth.models import User
from app.modules.payments.mpesa import MpesaError, parse_callback
from app.modules.payments.schemas import StartPaymentRequest, StartPaymentResponse
from app.modules.payments.service import PaymentsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])
booking_payments_router = APIRouter(prefix="/bookings", tags=["Payments"])


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


@router.post("/mpesa/callback/{secret}", include_in_schema=False)
async def mpesa_callback(secret: str, request: Request):
    """Daraja posts STK push results here (the URL comes from MPESA_CALLBACK_URL + secret).

    For now this verifies, parses and logs the result. Saving it against the
    payment and confirming the booking is BK-32 (docs/booking/BACKLOG.md).
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
    # Daraja retries until it gets this acknowledgement.
    return {"ResultCode": 0, "ResultDesc": "Accepted"}
