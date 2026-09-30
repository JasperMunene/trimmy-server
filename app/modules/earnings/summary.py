"""Earnings summary (fulfillment.json#get-earnings-summary). Pure: no database.

Mirrors the app's src/utils/earnings.ts rule for rule, so the screens look the
same when USE_MOCK_CHARGE is switched off:
- Only paid bookings count toward money. completed / noShow / cancelled count
  every booking in the range by status.
- A booking belongs to the range by its local start (date + time), [start, end).
- Buckets adapt to the span: 3-hour bars for a day, daily up to 16 days, weekly
  up to 70 days, monthly beyond; labelled like the app ("9 AM", "Mon", "Oct 5", "Oct").
- Services list every current service (0 rows included), amount per service line
  (price x quantity), sorted by amount then name.
- Transactions are one row per service line, newest first.
- paymentMethods attributes each paid booking's total to the method that settled it.

On top of the contract: bucket start/end and counts, a previous-period comparison,
a per-staff breakdown, commission, and the money outside sales (deposits,
cancellation fees kept, refunds owed and paid back).
"""
import uuid as uuid_pkg
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

METHODS = ("mpesa", "cash")


@dataclass(frozen=True)
class Line:
    service_id: Optional[uuid_pkg.UUID]
    name: str
    amount: int  # price x quantity


@dataclass(frozen=True)
class BookingRow:
    booking_id: uuid_pkg.UUID
    starts: datetime  # business local time, timezone-aware
    status: str
    payment_status: str
    total: int
    payment_method: Optional[str]  # what settled it: mpesa | cash | None
    customer_name: str
    lines: tuple[Line, ...]
    staff_id: Optional[uuid_pkg.UUID] = None
    staff_name: Optional[str] = None
    commission: int = 0
    deposit_paid: int = 0
    cancellation_fee: int = 0
    refund_amount: int = 0
    refund_status: Optional[str] = None

    @property
    def paid(self) -> bool:
        return self.payment_status == "paid"


@dataclass(frozen=True)
class PaymentRow:
    booking_id: uuid_pkg.UUID
    kind: str  # deposit | charge
    provider: str  # mpesa | cash
    amount: int


@dataclass
class Bucket:
    label: str
    start: datetime
    end: datetime
    amount: int = 0
    bookings_count: int = 0


@dataclass
class Summary:
    total_revenue: int
    completed: int
    no_show: int
    cancelled: int
    avg_ticket: int
    granularity: str
    buckets: list[Bucket]
    payment_methods: list[dict]
    services: list[dict]
    transactions: list[dict]
    # extras
    paid_bookings: int = 0
    commission: int = 0
    staff: list[dict] = field(default_factory=list)
    collected: list[dict] = field(default_factory=list)
    deposits_collected: int = 0
    cancellation_fees: int = 0
    refunds_due: int = 0
    refunds_paid: int = 0


def granularity_for(start: datetime, end: datetime) -> str:
    span_days = round((end - start).total_seconds() / 86400)
    if span_days <= 1:
        return "hour"
    if span_days <= 16:
        return "day"
    if span_days <= 70:
        return "week"
    return "month"


def _hour_label(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12} {'AM' if dt.hour < 12 else 'PM'}"


def make_buckets(start: datetime, end: datetime) -> tuple[str, list[Bucket]]:
    """Empty buckets covering [start, end), like getEarningsBuckets() in the app."""
    granularity = granularity_for(start, end)
    buckets: list[Bucket] = []
    cursor = start
    while cursor < end:
        if granularity == "hour":
            nxt, label = cursor + timedelta(hours=3), _hour_label(cursor)
        elif granularity == "day":
            nxt, label = cursor + timedelta(days=1), cursor.strftime("%a")
        elif granularity == "week":
            nxt, label = cursor + timedelta(days=7), f"{cursor.strftime('%b')} {cursor.day}"
        else:
            days_left = monthrange(cursor.year, cursor.month)[1] - cursor.day + 1
            nxt = (cursor + timedelta(days=days_left)).replace(hour=0, minute=0, second=0, microsecond=0)
            label = cursor.strftime("%b")
        nxt = min(nxt, end)
        buckets.append(Bucket(label=label, start=cursor, end=nxt))
        cursor = nxt
    return granularity, buckets


def summarise(
    *,
    bookings: list[BookingRow],
    payments: list[PaymentRow],
    services: list[tuple[uuid_pkg.UUID, str]],
    start: datetime,
    end: datetime,
    with_staff_breakdown: bool = True,
) -> Summary:
    """`bookings` may include ones outside [start, end); they're filtered here."""
    in_range = [b for b in bookings if start <= b.starts < end]
    paid = [b for b in in_range if b.paid]
    total = sum(b.total for b in paid)

    granularity, buckets = make_buckets(start, end)
    for b in paid:
        for bucket in buckets:
            if bucket.start <= b.starts < bucket.end:
                bucket.amount += b.total
                bucket.bookings_count += 1
                break

    methods = []
    for method in METHODS:
        matching = [b for b in paid if b.payment_method == method]
        if matching:
            methods.append({"method": method, "amount": sum(b.total for b in matching), "count": len(matching)})

    per_service: dict = {}
    for b in paid:
        for line in b.lines:
            row = per_service.setdefault(line.service_id, {"name": line.name, "amount": 0, "bookingsCount": 0})
            row["amount"] += line.amount
            row["bookingsCount"] += 1
    service_rows = [
        {"serviceId": sid, "name": name, **{k: per_service.get(sid, {}).get(k, 0) for k in ("amount", "bookingsCount")}}
        for sid, name in services
    ]
    current = {sid for sid, _ in services}
    # Revenue from services that have since been deleted still shows, so totals add up.
    service_rows += [
        {"serviceId": sid, "name": row["name"], "amount": row["amount"], "bookingsCount": row["bookingsCount"]}
        for sid, row in per_service.items() if sid not in current
    ]
    service_rows.sort(key=lambda r: (-r["amount"], r["name"].lower()))

    transactions = [
        {
            "bookingId": b.booking_id,
            "serviceId": line.service_id,
            "date": b.starts.date().isoformat(),
            "time": b.starts.strftime("%H:%M"),
            "customerName": b.customer_name,
            "serviceName": line.name,
            "amount": line.amount,
            "paymentMethod": b.payment_method,
        }
        for b in paid for line in b.lines
    ]
    transactions.sort(key=lambda t: (t["date"], t["time"]), reverse=True)

    staff_rows = []
    if with_staff_breakdown:
        per_staff: dict = {}
        for b in paid:
            row = per_staff.setdefault(b.staff_id, {"staffId": b.staff_id, "name": b.staff_name or "Unassigned",
                                                     "amount": 0, "bookingsCount": 0, "commission": 0})
            row["amount"] += b.total
            row["bookingsCount"] += 1
            row["commission"] += b.commission
        # Highest earners first; "Unassigned" after anyone with the same amount.
        staff_rows = sorted(per_staff.values(), key=lambda r: (-r["amount"], r["staffId"] is None, r["name"].lower()))

    in_range_ids = {b.booking_id for b in in_range}
    collected: dict = {}
    for p in payments:
        if p.booking_id in in_range_ids:
            row = collected.setdefault((p.provider, p.kind), {"method": p.provider, "kind": p.kind, "amount": 0, "count": 0})
            row["amount"] += p.amount
            row["count"] += 1

    return Summary(
        total_revenue=total,
        completed=sum(1 for b in in_range if b.status == "completed"),
        no_show=sum(1 for b in in_range if b.status == "no_show"),
        cancelled=sum(1 for b in in_range if b.status == "cancelled"),
        avg_ticket=round(total / len(paid)) if paid else 0,
        granularity=granularity,
        buckets=buckets,
        payment_methods=methods,
        services=service_rows,
        transactions=transactions,
        paid_bookings=len(paid),
        commission=sum(b.commission for b in paid),
        staff=staff_rows,
        collected=sorted(collected.values(), key=lambda r: (r["method"], r["kind"])),
        deposits_collected=sum(r["amount"] for r in collected.values() if r["kind"] == "deposit"),
        cancellation_fees=sum(b.cancellation_fee for b in in_range if b.status == "cancelled"),
        refunds_due=sum(b.refund_amount for b in in_range if b.refund_status == "due"),
        refunds_paid=sum(b.refund_amount for b in in_range if b.refund_status == "refunded"),
    )


def change_percent(current: int, previous: int) -> Optional[float]:
    """Change vs the previous period, one decimal; None when there's nothing to compare against."""
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)
