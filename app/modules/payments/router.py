import hmac
import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.modules.payments.mpesa import MpesaError, parse_callback

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])


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
