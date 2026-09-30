"""The pure aggregation (app/modules/earnings/summary.py): bucket rules and every total."""
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.earnings.summary import BookingRow, Line, PaymentRow, change_percent, make_buckets, summarise

NBO = ZoneInfo("Africa/Nairobi")
CUT, COLOUR, BEARD, GONE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
A, B = uuid.uuid4(), uuid.uuid4()
SERVICES = [(CUT, "Cut"), (COLOUR, "Colour"), (BEARD, "Beard Trim")]


def at(day: int, hour: int = 10, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=NBO)


def row(starts, total=800, status="completed", paid=True, method="cash", lines=None, **extra):
    return BookingRow(
        booking_id=extra.pop("booking_id", uuid.uuid4()), starts=starts, status=status,
        payment_status="paid" if paid else "unpaid", total=total, payment_method=method if paid else None,
        customer_name=extra.pop("customer_name", "Amina"),
        lines=tuple(lines or [Line(CUT, "Cut", total)]), **extra,
    )


# ─── Buckets, exactly like the app ───────────────────────────


def labels(start, end):
    return make_buckets(start, end)


def test_one_day_is_eight_three_hour_bars():
    granularity, buckets = labels(at(5, 0), at(6, 0))
    assert granularity == "hour"
    assert [b.label for b in buckets] == ["12 AM", "3 AM", "6 AM", "9 AM", "12 PM", "3 PM", "6 PM", "9 PM"]


def test_a_week_is_daily_by_weekday():
    granularity, buckets = labels(at(5, 0), at(12, 0))  # Mon 5 Oct .. Sun 11 Oct
    assert granularity == "day"
    assert [b.label for b in buckets] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@pytest.mark.parametrize("days,expected", [(16, "day"), (17, "week"), (70, "week"), (71, "month")])
def test_granularity_thresholds(days, expected):
    start = at(1, 0)
    assert labels(start, start + timedelta(days=days))[0] == expected


def test_weekly_labels_and_a_short_last_week():
    granularity, buckets = labels(at(1, 0), at(31, 0))  # 30 days
    assert granularity == "week"
    assert [b.label for b in buckets] == ["Oct 1", "Oct 8", "Oct 15", "Oct 22", "Oct 29"]
    assert buckets[-1].end == at(31, 0)  # clamped to the range


def test_months_start_on_the_first_after_a_partial_one():
    granularity, buckets = labels(at(15, 0, month=9), at(10, 0, month=12))
    assert granularity == "month"
    assert [b.label for b in buckets] == ["Sep", "Oct", "Nov", "Dec"]
    assert buckets[1].start == at(1, 0, month=10)


# ─── Totals ──────────────────────────────────────────────────


@pytest.fixture
def week():
    s = summarise(
        bookings=[
            row(at(5, 10), 800, method="cash", staff_id=A, staff_name="Wanjiku", commission=320),
            row(at(5, 14), 2500, method="mpesa", staff_id=A, staff_name="Wanjiku", commission=1000,
                lines=[Line(COLOUR, "Colour", 2000), Line(CUT, "Cut", 500)], deposit_paid=300),
            row(at(7, 11), 700, method="mpesa", staff_id=None, lines=[Line(GONE, "Old Service", 700)]),
            row(at(8, 9), 800, status="no_show", paid=False),
            row(at(9, 9), 800, status="cancelled", paid=False, cancellation_fee=300, refund_amount=0),
            row(at(9, 12), 800, status="cancelled", paid=False, refund_amount=300, refund_status="due"),
            row(at(10, 12), 800, status="confirmed", paid=False),  # not paid yet: no money
            row(at(20, 10), 9999),  # outside the range
        ],
        payments=[],
        services=SERVICES,
        start=at(5, 0),
        end=at(12, 0),
    )
    return s


def test_money_counts_only_paid_bookings(week):
    assert (week.total_revenue, week.paid_bookings, week.avg_ticket) == (4000, 3, 1333)
    assert (week.completed, week.no_show, week.cancelled) == (3, 1, 2)


def test_buckets_get_the_money_by_day(week):
    assert [(b.label, b.amount, b.bookings_count) for b in week.buckets] == [
        ("Mon", 3300, 2), ("Tue", 0, 0), ("Wed", 700, 1), ("Thu", 0, 0), ("Fri", 0, 0), ("Sat", 0, 0), ("Sun", 0, 0),
    ]


def test_payment_methods_attribute_whole_bookings(week):
    assert week.payment_methods == [{"method": "mpesa", "amount": 3200, "count": 2}, {"method": "cash", "amount": 800, "count": 1}]


def test_services_include_zero_rows_and_deleted_ones(week):
    assert week.services == [
        {"serviceId": COLOUR, "name": "Colour", "amount": 2000, "bookingsCount": 1},
        {"serviceId": CUT, "name": "Cut", "amount": 1300, "bookingsCount": 2},
        {"serviceId": GONE, "name": "Old Service", "amount": 700, "bookingsCount": 1},
        {"serviceId": BEARD, "name": "Beard Trim", "amount": 0, "bookingsCount": 0},
    ]


def test_transactions_are_per_line_newest_first(week):
    assert [(t["date"], t["time"], t["serviceName"], t["amount"]) for t in week.transactions] == [
        ("2026-10-07", "11:00", "Old Service", 700),
        ("2026-10-05", "14:00", "Colour", 2000),
        ("2026-10-05", "14:00", "Cut", 500),
        ("2026-10-05", "10:00", "Cut", 800),
    ]


def test_staff_breakdown_and_commission(week):
    assert week.commission == 1320
    assert [(s["name"], s["amount"], s["bookingsCount"], s["commission"]) for s in week.staff] == [
        ("Wanjiku", 3300, 2, 1320), ("Unassigned", 700, 1, 0),
    ]


def test_money_outside_sales(week):
    assert (week.cancellation_fees, week.refunds_due, week.refunds_paid) == (300, 300, 0)


def test_collected_by_method_and_kind():
    b1, b2 = uuid.uuid4(), uuid.uuid4()
    s = summarise(
        bookings=[row(at(5), booking_id=b1, method="cash"), row(at(6), booking_id=b2, method="mpesa")],
        payments=[PaymentRow(b1, "deposit", "mpesa", 300), PaymentRow(b1, "charge", "cash", 500),
                  PaymentRow(b2, "charge", "mpesa", 800), PaymentRow(uuid.uuid4(), "charge", "cash", 99)],
        services=SERVICES, start=at(5, 0), end=at(12, 0),
    )
    assert s.collected == [
        {"method": "cash", "kind": "charge", "amount": 500, "count": 1},
        {"method": "mpesa", "kind": "charge", "amount": 800, "count": 1},
        {"method": "mpesa", "kind": "deposit", "amount": 300, "count": 1},
    ]
    assert s.deposits_collected == 300


def test_empty_range():
    s = summarise(bookings=[], payments=[], services=SERVICES, start=at(5, 0), end=at(6, 0))
    assert (s.total_revenue, s.avg_ticket, s.payment_methods, s.transactions) == (0, 0, [], [])
    assert len(s.services) == 3 and all(r["amount"] == 0 for r in s.services)


@pytest.mark.parametrize("current,previous,expected", [(1500, 1000, 50.0), (500, 1000, -50.0), (1000, 0, None), (0, 0, None)])
def test_change_percent(current, previous, expected):
    assert change_percent(current, previous) == expected
