import time as clock
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.modules.booking.availability import (
    ExistingBooking,
    Settings,
    StaffInfo,
    day_slots,
    open_days,
    slot_is_free,
)

NBO = ZoneInfo("Africa/Nairobi")
CUT, COLOUR, NAILS = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

WEEK = {day: {"open": "09:00", "close": "17:00"} for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday")}
WEEK["sunday"] = None

# A Monday a few days out, and "now" the Friday before at 12:00 Nairobi.
MONDAY = date(2026, 9, 28)
assert MONDAY.weekday() == 0
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=NBO)


def at(hhmm: str, day: date = MONDAY) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=NBO)


def booking(start: str, end: str, staff=None, status="confirmed", services=(CUT,), day=MONDAY, expires_at=None):
    return ExistingBooking(
        start=at(start, day), end=at(end, day), status=status, staff_id=staff,
        service_ids=frozenset(services), expires_at=expires_at,
    )


def slots(**overrides):
    kwargs = dict(
        day=MONDAY, weekly_hours=WEEK, staff=[StaffInfo(A)], bookings=[], service_ids={CUT},
        duration_minutes=60, settings=Settings(), now=NOW,
    )
    kwargs.update(overrides)
    return day_slots(**kwargs)


def times(result):
    return [s.time for s in result]


# ─── Opening hours and the grid ──────────────────────────────


def test_open_day_on_15_minute_grid():
    result = times(slots())
    assert result[0] == "09:00"
    assert result[1] == "09:15"
    assert result[-1] == "16:00"  # 60 minutes must end by 17:00
    assert len(result) == 29


def test_closed_day():
    sunday = MONDAY - timedelta(days=1)
    assert slots(day=sunday) == []


def test_no_working_hours_at_all():
    assert slots(weekly_hours=None) == []


def test_duration_longer_than_the_day():
    assert slots(duration_minutes=9 * 60) == []


def test_opening_off_grid_rounds_up():
    week = {**WEEK, "monday": {"open": "09:10", "close": "12:00"}}
    assert times(slots(weekly_hours=week))[0] == "09:15"


def test_open_until_midnight():
    week = {**WEEK, "monday": {"open": "20:00", "close": "24:00"}}
    assert times(slots(weekly_hours=week))[-1] == "23:00"


def test_30_minute_grid():
    assert times(slots(settings=Settings(slot_interval_minutes=30)))[:3] == ["09:00", "09:30", "10:00"]


# ─── Lead time and horizon ───────────────────────────────────


def test_lead_time_on_the_current_day():
    now = at("10:05")
    result = times(slots(now=now))  # earliest = 10:35
    assert result[0] == "10:45"


def test_past_day_has_nothing():
    assert slots(now=at("08:00", MONDAY + timedelta(days=1))) == []


def test_max_days_ahead():
    far = NOW.date() + timedelta(days=30)
    assert slots(day=far, settings=Settings(max_days_ahead=30), weekly_hours={k: {"open": "09:00", "close": "17:00"} for k in WEEK}) != []
    assert slots(day=far + timedelta(days=1), settings=Settings(max_days_ahead=30)) == []


def test_now_in_utc_is_converted_to_business_time():
    # 06:30 UTC = 09:30 Nairobi on Monday.
    now = datetime(2026, 9, 28, 6, 30, tzinfo=timezone.utc)
    assert times(slots(now=now, settings=Settings(min_lead_minutes=0)))[0] == "09:30"


# ─── Staff eligibility ───────────────────────────────────────


def test_staff_not_working_that_weekday():
    staff = [StaffInfo(A, working_days=frozenset({"tuesday"}))]
    assert slots(staff=staff) == []
    assert slots(staff=staff, day=MONDAY + timedelta(days=1)) != []


def test_staff_must_perform_every_service():
    staff = [StaffInfo(A, performs_all_services=False, service_ids=frozenset({CUT}))]
    assert slots(staff=staff) != []
    assert slots(staff=staff, service_ids={CUT, COLOUR}) == []


def test_requested_staff_who_is_not_eligible():
    staff = [StaffInfo(A), StaffInfo(B, performs_all_services=False, service_ids=frozenset())]
    assert slots(staff=staff, staff_id=B) == []


def test_any_staff_lists_everyone_free():
    staff = [StaffInfo(A), StaffInfo(B)]
    first = slots(staff=staff, bookings=[booking("09:00", "10:00", staff=B)])[0]
    assert first.time == "09:00" and first.staff_ids == (A,)


# ─── Existing bookings ───────────────────────────────────────


def test_assigned_booking_blocks_that_staff_only():
    staff = [StaffInfo(A), StaffInfo(B)]
    taken = [booking("10:00", "11:00", staff=A)]
    assert "10:00" not in times(slots(staff=staff, bookings=taken, staff_id=A))
    assert "10:00" in times(slots(staff=staff, bookings=taken, staff_id=B))
    assert "10:00" in times(slots(staff=staff, bookings=taken))


def test_back_to_back_is_allowed_without_buffer():
    result = times(slots(bookings=[booking("10:00", "11:00", staff=A)]))
    assert "09:00" in result and "11:00" in result
    assert "09:15" not in result and "10:45" not in result


def test_buffer_on_both_sides():
    result = times(slots(
        bookings=[booking("10:30", "11:30", staff=A)], settings=Settings(buffer_minutes=15), duration_minutes=60,
    ))
    assert "09:15" in result  # 09:15-10:15, its buffer ends exactly at 10:30
    assert "09:30" not in result  # 09:30-10:30 + 15-minute buffer runs into the booking
    assert "11:30" not in result  # the existing booking's buffer runs until 11:45
    assert "11:45" in result


def test_inactive_bookings_do_not_block():
    for status in ("cancelled", "completed", "no_show"):
        assert "10:00" in times(slots(bookings=[booking("10:00", "11:00", staff=A, status=status)]))


def test_pending_payment_blocks_until_its_hold_expires():
    held = booking("10:00", "11:00", staff=A, status="pending_payment", expires_at=NOW + timedelta(minutes=5))
    assert "10:00" not in times(slots(bookings=[held]))
    expired = booking("10:00", "11:00", staff=A, status="pending_payment", expires_at=NOW - timedelta(minutes=1))
    assert "10:00" in times(slots(bookings=[expired]))


# ─── Unassigned bookings (D4) use capacity ───────────────────


def test_unassigned_booking_takes_one_of_two_staff():
    staff = [StaffInfo(A), StaffInfo(B)]
    one = [booking("10:00", "11:00")]
    assert "10:00" in times(slots(staff=staff, bookings=one))
    two = one + [booking("10:00", "11:00")]
    assert "10:00" not in times(slots(staff=staff, bookings=two))


def test_unassigned_booking_blocks_a_specific_request_when_only_that_person_can_cover_it():
    # A does cuts only, B does nails only. An unassigned cut at 10:00 can only be A's.
    staff = [
        StaffInfo(A, performs_all_services=False, service_ids=frozenset({CUT})),
        StaffInfo(B, performs_all_services=False, service_ids=frozenset({NAILS, CUT})),
    ]
    only_a = [StaffInfo(A, performs_all_services=False, service_ids=frozenset({CUT})),
              StaffInfo(B, performs_all_services=False, service_ids=frozenset({NAILS}))]
    unassigned_cut = [booking("10:00", "11:00", services=(CUT,))]
    # With B unable to cut, booking A for a cut at 10:00 would leave the unassigned cut uncovered.
    assert "10:00" not in times(slots(staff=only_a, bookings=unassigned_cut, staff_id=A))
    # A nails booking with B is fine.
    assert "10:00" in times(slots(staff=only_a, bookings=unassigned_cut, staff_id=B, service_ids={NAILS}))
    # If B can also cut, A can be booked and B covers the unassigned cut.
    assert "10:00" in times(slots(staff=staff, bookings=unassigned_cut, staff_id=A))


def test_unassigned_capacity_respects_assigned_bookings():
    staff = [StaffInfo(A), StaffInfo(B)]
    taken = [booking("10:00", "11:00", staff=A), booking("10:00", "11:00")]  # A busy, B covers the unassigned one
    assert "10:00" not in times(slots(staff=staff, bookings=taken))


def test_three_staff_three_unassigned():
    staff = [StaffInfo(A), StaffInfo(B), StaffInfo(C)]
    held = [booking("10:00", "11:00") for _ in range(2)]
    assert "10:00" in times(slots(staff=staff, bookings=held))
    assert "10:00" not in times(slots(staff=staff, bookings=held + [booking("10:00", "11:00")]))


# ─── Helpers ─────────────────────────────────────────────────


def test_slot_is_free_matches_day_slots():
    common = dict(weekly_hours=WEEK, staff=[StaffInfo(A)], bookings=[booking("10:00", "11:00", staff=A)],
                  service_ids={CUT}, duration_minutes=60, settings=Settings(), now=NOW)
    assert slot_is_free(starts_at=at("11:00"), **common)
    assert not slot_is_free(starts_at=at("10:30"), **common)
    assert not slot_is_free(starts_at=at("11:05"), **common)  # off the grid
    # Given in UTC, same instant as 11:00 Nairobi.
    assert slot_is_free(starts_at=at("11:00").astimezone(timezone.utc), **common)


def test_open_days():
    days = open_days(
        start_day=MONDAY - timedelta(days=1), days=3, weekly_hours=WEEK, staff=[StaffInfo(A)],
        bookings=[booking("09:00", "17:00", staff=A)], service_ids={CUT}, duration_minutes=60,
        settings=Settings(), now=NOW,
    )
    assert [(d.is_open, d.has_slots) for d in days] == [(False, False), (True, False), (True, True)]


def test_rejects_non_positive_duration():
    with pytest.raises(ValueError):
        slots(duration_minutes=0)


def test_fast_enough():
    staff = [StaffInfo(uuid.uuid4()) for _ in range(10)]
    bookings = []
    for i, member in enumerate(staff):
        for j in range(4):
            start = f"{9 + 2 * j:02d}:{(i % 4) * 15:02d}"
            end_h, end_m = divmod(int(start[:2]) * 60 + int(start[3:]) + 45, 60)
            bookings.append(booking(start, f"{end_h:02d}:{end_m:02d}", staff=member.id))
    started = clock.perf_counter()
    slots(staff=staff, bookings=bookings)
    assert clock.perf_counter() - started < 0.05
