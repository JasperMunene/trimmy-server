"""Price and deposit calculator (BK-21). Pure: no database.

The server always computes these from its own service prices and the
business's deposit policy, never from what the client sends (D7).

    quote = quote_booking(lines, policy)
    quote.total, quote.deposit, quote.duration_minutes

Deposit rules
- No policy, or deposit not required → no deposit.
- The deposit is based on the whole cart ("all_services"), or only on the
  lines for the policy's services ("selected_services"). If none of those
  services are in the cart, there's no deposit.
- "fixed": the policy amount, capped at that base.
- "percent": percent of the base, rounded up to a whole unit.
- A deposit that works out to 0 means no deposit.
"""
import math
import uuid as uuid_pkg
from dataclasses import dataclass
from typing import Iterable, Optional


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class Line:
    service_id: uuid_pkg.UUID
    price_amount: int  # whole units of currency
    duration_minutes: int
    quantity: int = 1
    currency: str = "KES"


@dataclass(frozen=True)
class DepositPolicy:
    required: bool
    type: Optional[str] = None  # fixed | percent
    amount: Optional[int] = None  # for fixed
    percent: Optional[int] = None  # for percent, 0-100
    applies_to: Optional[str] = "all_services"  # all_services | selected_services
    service_ids: frozenset = frozenset()  # for selected_services


@dataclass(frozen=True)
class Quote:
    total: int
    currency: str
    duration_minutes: int
    deposit: Optional[int]  # None = no deposit


def quote_booking(lines: Iterable[Line], policy: Optional[DepositPolicy]) -> Quote:
    lines = list(lines)
    if not lines:
        raise PricingError("A booking needs at least one service")
    if any(line.quantity < 1 for line in lines):
        raise PricingError("quantity must be at least 1")
    currencies = {line.currency for line in lines}
    if len(currencies) > 1:
        raise PricingError(f"Services are priced in different currencies: {', '.join(sorted(currencies))}")

    total = sum(line.price_amount * line.quantity for line in lines)
    duration = sum(line.duration_minutes * line.quantity for line in lines)
    return Quote(total=total, currency=currencies.pop(), duration_minutes=duration, deposit=_deposit(lines, total, policy))


def _deposit(lines: list[Line], total: int, policy: Optional[DepositPolicy]) -> Optional[int]:
    if policy is None or not policy.required:
        return None

    if policy.applies_to == "selected_services":
        wanted = {str(i) for i in policy.service_ids}
        base = sum(line.price_amount * line.quantity for line in lines if str(line.service_id) in wanted)
    else:
        base = total
    if base <= 0:
        return None

    if policy.type == "percent":
        deposit = math.ceil(base * (policy.percent or 0) / 100)
    elif policy.type == "fixed":
        deposit = min(policy.amount or 0, base)
    else:
        raise PricingError(f"Unknown deposit type {policy.type!r}")
    return deposit or None
