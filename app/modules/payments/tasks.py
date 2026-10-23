import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.registry import periodic

logger = logging.getLogger(__name__)

# Ask M-Pesa about payments still pending after this long (the callback is usually seconds).
QUERY_AFTER = timedelta(seconds=60)
# Give up on a payment after this long: the prompt has certainly timed out on the phone.
GIVE_UP_AFTER = timedelta(minutes=3)
BATCH = 50


@periodic("payments.reconcile_pending", every=60)
async def reconcile_pending_job(db: AsyncSession) -> None:
    """Resolve payments whose callback never came (BK-33), with STK Query.

    Applies results through the same code as the callback, so whichever
    arrives first wins and the other is a no-op.
    """
    from app.modules.payments.models import Payment, PaymentStatus
    from app.modules.payments.mpesa import MpesaConfigError, MpesaError, PaymentResult
    from app.modules.payments.results import apply_result
    from app.modules.payments.service import get_provider

    now = datetime.now(timezone.utc)
    # Plain tuples: apply_result() commits, which may expire ORM objects mid-loop.
    stuck = (
        await db.execute(
            select(Payment.id, Payment.checkout_request_id, Payment.created_at)
            .where(Payment.status == PaymentStatus.PENDING, Payment.created_at <= now - QUERY_AFTER)
            .order_by(Payment.created_at)
            .limit(BATCH)
        )
    ).all()
    if not stuck:
        return
    try:
        provider = get_provider()
    except MpesaConfigError as e:
        logger.error(f"Can't reconcile {len(stuck)} pending payment(s): {e.message}")
        return

    for payment_id, checkout_id, created_at in stuck:
        created = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        overdue = now - created > GIVE_UP_AFTER

        if not checkout_id:
            # The push never got a CheckoutRequestID (crashed mid-call); nothing to ask M-Pesa about.
            if overdue:
                payment = await db.get(Payment, payment_id)
                if payment is not None and payment.status == PaymentStatus.PENDING:
                    payment.status, payment.failure_reason, payment.settled_at = PaymentStatus.FAILED, "provider_error", now
                    await db.commit()
            continue

        try:
            result = await provider.check_payment(checkout_id)
        except MpesaError as e:
            logger.warning(f"STK query for payment {payment_id} failed: [{e.code}] {e.message}")
            result = None
        if result is None or result.status == "pending":
            if not overdue:
                continue
            result = PaymentResult("timeout", "timeout", None, "No result from M-Pesa in time", checkout_id)
        await apply_result(db, result, now)
        logger.info(f"Reconciled payment {payment_id}: {result.status}")
