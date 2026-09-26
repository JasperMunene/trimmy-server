"""What cancelling a booking costs (BK-60). Pure: no database.

Deposits sit in the business's own account (D1), so a refund is something the
business owes and pays back by hand (D10); this works out how much.

- Customer cancels at least `free_hours` before the start: the whole paid deposit is refunded.
- Later than that: a fee of `late_fee_percent` of the booking total is kept, capped at the
  deposit (there's nothing else to take it from); the rest of the deposit is refunded.
- No deposit paid: cancelling is free and there's nothing to refund.
- The business cancels: the whole paid deposit is refunded.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

CANCELLABLE_BY_CUSTOMER = ("pending_payment", "confirmed")


@dataclass(frozen=True)
class CancellationOutcome:
    can_cancel: bool
    reason_blocked: Optional[str]  # why not, when can_cancel is False
    free_until: datetime  # cancel before this for a full refund
    within_free_window: bool
    deposit_paid: int  # 0 if none
    fee: int  # kept by the business
    refund: int  # owed back to the customer


def outcome(
    *,
    status: str,
    starts_at: datetime,
    total: int,
    deposit_paid: int,
    free_hours: int,
    late_fee_percent: int,
    now: datetime,
    by_business: bool = False,
) -> CancellationOutcome:
    free_until = starts_at - timedelta(hours=free_hours)
    within = now <= free_until

    blocked = None
    if status not in CANCELLABLE_BY_CUSTOMER and not (by_business and status == "in_progress"):
        blocked = f"A {status} booking can't be cancelled"
    elif not by_business and now >= starts_at:
        blocked = "This appointment has already started"

    if by_business or within or deposit_paid == 0:
        fee = 0
    else:
        fee = min(deposit_paid, math.ceil(total * late_fee_percent / 100))
    return CancellationOutcome(
        can_cancel=blocked is None,
        reason_blocked=blocked,
        free_until=free_until,
        within_free_window=within,
        deposit_paid=deposit_paid,
        fee=fee,
        refund=deposit_paid - fee,
    )
