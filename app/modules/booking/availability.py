"""Availability engine: which start times can be booked (docs/booking/PLAN.md, section 4.3).

Pure functions, no database or clock: callers pass in the business's hours,
staff, the bookings that might conflict, the settings and `now`. The same code
answers "what can I show the customer?" and, inside the booking transaction,
"is this exact slot still free?", so the two can't disagree.

    slots = day_slots(day=date(2026, 9, 28), weekly_hours=..., staff=[...], bookings=[...],
                      service_ids={cut_id}, duration_minutes=45, settings=..., now=utc_now)
    [Slot(time="09:00", starts_at=..., staff_ids=(a, b)), ...]

Rules
- A day is bookable from today (business local time) to today + max_days_ahead.
- Start times sit on a grid of slot_interval_minutes from midnight, within
  opening hours; the booking must end by closing time.
- A start time must be at least min_lead_minutes from now.
- Staff are eligible if they work that weekday and perform every requested service.
- Every booking blocks [start, end + buffer). Only active bookings count:
  pending_payment (until its hold expires), confirmed, in_progress.
- Unassigned bookings ("any available", D4) block no one in particular but use
  capacity: the new booking plus every unassigned booking overlapping it must
  each get a different eligible, free staff member. This is conservative: two
  unassigned bookings that overlap the new one but not each other still count
  as needing two people, so a slot is occasionally hidden, never overbooked.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

ACTIVE_STATUSES = frozenset({"pending_payment", "confirmed", "in_progress"})


@dataclass(frozen=True)
class StaffInfo:
    id: UUID
    # None = works whenever the business is open.
    working_days: Optional[frozenset[str]] = None
    performs_all_services: bool = True
    service_ids: frozenset[UUID] = frozenset()

    def works_on(self, weekday: str) -> bool:
        return self.working_days is None or weekday in self.working_days

    def performs(self, service_ids: Iterable[UUID]) -> bool:
        return self.performs_all_services or set(service_ids) <= self.service_ids


@dataclass(frozen=True)
class ExistingBooking:
    start: datetime  # timezone-aware
    end: datetime
    status: str
    staff_id: Optional[UUID] = None  # None = unassigned
    service_ids: frozenset[UUID] = frozenset()
    expires_at: Optional[datetime] = None  # hold expiry for pending_payment

    def is_active(self, now: datetime) -> bool:
        if self.status not in ACTIVE_STATUSES:
            return False
        return not (self.status == "pending_payment" and self.expires_at is not None and self.expires_at <= now)


@dataclass(frozen=True)
class Settings:
    timezone: str = "Africa/Nairobi"
    slot_interval_minutes: int = 15
    buffer_minutes: int = 0
    min_lead_minutes: int = 30
    max_days_ahead: int = 30


@dataclass(frozen=True)
class Slot:
    time: str  # "HH:MM", business local time
    starts_at: datetime  # timezone-aware, business local time
    # Eligible staff free for this slot. For "any available" the slot is offered
    # only if capacity allows; this list is informational (who could take it).
    staff_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class DayAvailability:
    date: date
    is_open: bool  # the business has hours that weekday
    has_slots: bool


def day_slots(
    *,
    day: date,
    weekly_hours: Optional[dict],
    staff: list[StaffInfo],
    bookings: list[ExistingBooking],
    service_ids: set[UUID],
    duration_minutes: int,
    settings: Settings,
    now: datetime,
    staff_id: Optional[UUID] = None,
) -> list[Slot]:
    """Bookable start times on `day` for these services, optionally with one staff member."""
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive")
    tz = ZoneInfo(settings.timezone)
    today = now.astimezone(tz).date()
    if day < today or day > today + timedelta(days=settings.max_days_ahead):
        return []

    window = _opening_window(day, weekly_hours, tz)
    if window is None:
        return []
    opens, closes = window

    weekday = WEEKDAYS[day.weekday()]
    eligible = [s for s in staff if s.works_on(weekday) and s.performs(service_ids)]
    if staff_id is not None and not any(s.id == staff_id for s in eligible):
        return []
    if not eligible:
        return []

    active = [b for b in bookings if b.is_active(now)]
    step = timedelta(minutes=settings.slot_interval_minutes)
    duration = timedelta(minutes=duration_minutes)
    buffer = timedelta(minutes=settings.buffer_minutes)
    earliest = now + timedelta(minutes=settings.min_lead_minutes)

    slots = []
    start = _first_grid_point(opens, settings.slot_interval_minutes)
    while start + duration <= closes:
        if start >= earliest:
            slot = _check(start, start + duration, buffer, weekday, eligible, staff, active, service_ids, staff_id)
            if slot is not None:
                slots.append(Slot(time=start.strftime("%H:%M"), starts_at=start, staff_ids=slot))
        start += step
    return slots


def slot_is_free(*, starts_at: datetime, **kwargs) -> bool:
    """Whether this exact start is still bookable (used inside the booking transaction)."""
    local = starts_at.astimezone(ZoneInfo(kwargs["settings"].timezone))
    return any(s.starts_at == local for s in day_slots(day=local.date(), **kwargs))


def staff_can_take(
    *,
    start: datetime,
    end: datetime,
    staff_id: UUID,
    staff: list[StaffInfo],
    bookings: list[ExistingBooking],
    service_ids: set[UUID],
    settings: Settings,
    now: datetime,
) -> bool:
    """Whether `staff_id` can take an existing booking at [start, end) (assigning it, BK-26).

    Same staff and capacity rules as a new booking, but no grid, lead-time or
    opening-hours checks: the booking already exists. `bookings` must not
    include the booking being assigned.
    """
    weekday = WEEKDAYS[start.astimezone(ZoneInfo(settings.timezone)).weekday()]
    eligible = [s for s in staff if s.works_on(weekday) and s.performs(service_ids)]
    if not any(s.id == staff_id for s in eligible):
        return False
    active = [b for b in bookings if b.is_active(now)]
    buffer = timedelta(minutes=settings.buffer_minutes)
    return _check(start, end, buffer, weekday, eligible, staff, active, service_ids, staff_id) is not None


def open_days(
    *,
    start_day: date,
    days: int,
    weekly_hours: Optional[dict],
    staff: list[StaffInfo],
    bookings: list[ExistingBooking],
    service_ids: set[UUID],
    duration_minutes: int,
    settings: Settings,
    now: datetime,
    staff_id: Optional[UUID] = None,
) -> list[DayAvailability]:
    """For the day strip: which of the next `days` days are open and have any slot."""
    tz = ZoneInfo(settings.timezone)
    result = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        is_open = _opening_window(day, weekly_hours, tz) is not None
        has_slots = is_open and bool(
            day_slots(
                day=day, weekly_hours=weekly_hours, staff=staff, bookings=bookings, service_ids=service_ids,
                duration_minutes=duration_minutes, settings=settings, now=now, staff_id=staff_id,
            )
        )
        result.append(DayAvailability(date=day, is_open=is_open, has_slots=has_slots))
    return result


# ─── internals ───────────────────────────────────────────────


def _opening_window(day: date, weekly_hours: Optional[dict], tz: ZoneInfo) -> Optional[tuple[datetime, datetime]]:
    hours = (weekly_hours or {}).get(WEEKDAYS[day.weekday()])
    if not isinstance(hours, dict) or not hours.get("open") or not hours.get("close"):
        return None
    opens = datetime.combine(day, _parse_hhmm(hours["open"]), tz)
    if hours["close"] == "24:00":
        closes = datetime.combine(day + timedelta(days=1), time(0, 0), tz)
    else:
        closes = datetime.combine(day, _parse_hhmm(hours["close"]), tz)
    return (opens, closes) if closes > opens else None


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _first_grid_point(opens: datetime, interval: int) -> datetime:
    minutes = opens.hour * 60 + opens.minute
    aligned = -(-minutes // interval) * interval  # round up to the grid
    return opens.replace(hour=0, minute=0) + timedelta(minutes=aligned)


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def _check(
    start: datetime,
    end: datetime,
    buffer: timedelta,
    weekday: str,
    eligible: list[StaffInfo],
    all_staff: list[StaffInfo],
    active: list[ExistingBooking],
    service_ids: set[UUID],
    staff_id: Optional[UUID],
) -> Optional[tuple[UUID, ...]]:
    """The free eligible staff if this start can be booked, else None."""
    block_end = end + buffer

    def free_for(member: StaffInfo, b_start: datetime, b_end: datetime) -> bool:
        return not any(
            b.staff_id == member.id and _overlaps(b_start, b_end, b.start, b.end + buffer) for b in active
        )

    free = [s for s in eligible if free_for(s, start, block_end)]
    if staff_id is not None:
        free = [s for s in free if s.id == staff_id]
    if not free:
        return None

    # Capacity: unassigned bookings overlapping this slot each need their own person.
    unassigned = [b for b in active if b.staff_id is None and _overlaps(start, block_end, b.start, b.end + buffer)]
    if unassigned:
        demands: list[list[UUID]] = []
        for b in unassigned:
            demands.append([
                s.id for s in all_staff
                if s.works_on(weekday) and s.performs(b.service_ids) and free_for(s, b.start, b.end + buffer)
            ])
        if staff_id is not None:
            # The new booking takes this person; the unassigned ones must fit around them.
            demands = [[sid for sid in d if sid != staff_id] for d in demands]
        else:
            demands.append([s.id for s in free])
        if not _can_match(demands):
            return None

    return tuple(s.id for s in free)


def _can_match(demands: list[list[UUID]]) -> bool:
    """Can every demand get a distinct staff member from its list? (Kuhn's augmenting paths.)"""
    owner: dict[UUID, int] = {}

    def assign(i: int, seen: set) -> bool:
        for sid in demands[i]:
            if sid in seen:
                continue
            seen.add(sid)
            if sid not in owner or assign(owner[sid], seen):
                owner[sid] = i
                return True
        return False

    return all(assign(i, set()) for i in range(len(demands)))
