import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.booking import availability
from app.modules.booking.models import ACTIVE_STATUSES, Booking
from app.modules.booking.schemas import (
    DayAvailabilityResponse,
    DaySlotsResponse,
    DaysResponse,
    SlotResponse,
)
from app.modules.business.models import Business, BusinessStatus, Service, Staff, StaffService

MAX_DAYS = 31


def _not_found(message: str = "No published business found for this id") -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "not_found", "message": message})


def _validation_error(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": "validation_error", "message": message})


@dataclass
class AvailabilityContext:
    business: Business
    staff: list[availability.StaffInfo]
    settings: availability.Settings
    service_ids: set[uuid_pkg.UUID]
    duration_minutes: int
    services: dict[uuid_pkg.UUID, Service]


class AvailabilityService:
    def __init__(self, db: AsyncSession, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.now(timezone.utc)

    async def day(
        self, business_id: str, day: date, service_ids: list[uuid_pkg.UUID], staff_id: Optional[uuid_pkg.UUID]
    ) -> DaySlotsResponse:
        ctx = await self.context(business_id, service_ids, staff_id)
        tz = ZoneInfo(ctx.settings.timezone)
        start = datetime.combine(day, datetime.min.time(), tz)
        slots = availability.day_slots(
            day=day,
            weekly_hours=ctx.business.working_hours.weekly_hours if ctx.business.working_hours else None,
            staff=ctx.staff,
            bookings=await self.existing_bookings(ctx, start, start + timedelta(days=1)),
            service_ids=ctx.service_ids,
            duration_minutes=ctx.duration_minutes,
            settings=ctx.settings,
            now=self.now,
            staff_id=staff_id,
        )
        return DaySlotsResponse(
            date=day,
            timezone=ctx.settings.timezone,
            durationMinutes=ctx.duration_minutes,
            slots=[SlotResponse(time=s.time, startsAt=s.starts_at, staffIds=list(s.staff_ids)) for s in slots],
        )

    async def days(
        self,
        business_id: str,
        start_day: Optional[date],
        days: int,
        service_ids: list[uuid_pkg.UUID],
        staff_id: Optional[uuid_pkg.UUID],
    ) -> DaysResponse:
        if not 1 <= days <= MAX_DAYS:
            raise _validation_error(f"days must be between 1 and {MAX_DAYS}")
        ctx = await self.context(business_id, service_ids, staff_id)
        tz = ZoneInfo(ctx.settings.timezone)
        start_day = start_day or self.now.astimezone(tz).date()
        start = datetime.combine(start_day, datetime.min.time(), tz)
        end = datetime.combine(date.fromordinal(start_day.toordinal() + days), datetime.min.time(), tz)
        result = availability.open_days(
            start_day=start_day,
            days=days,
            weekly_hours=ctx.business.working_hours.weekly_hours if ctx.business.working_hours else None,
            staff=ctx.staff,
            bookings=await self.existing_bookings(ctx, start, end),
            service_ids=ctx.service_ids,
            duration_minutes=ctx.duration_minutes,
            settings=ctx.settings,
            now=self.now,
            staff_id=staff_id,
        )
        return DaysResponse(
            timezone=ctx.settings.timezone,
            durationMinutes=ctx.duration_minutes,
            days=[DayAvailabilityResponse(date=d.date, isOpen=d.is_open, hasSlots=d.has_slots) for d in result],
        )

    async def context(
        self,
        business_id: str,
        service_ids: list[uuid_pkg.UUID],
        staff_id: Optional[uuid_pkg.UUID],
        *,
        for_existing_booking: bool = False,
    ) -> "AvailabilityContext":
        """Load what availability needs. for_existing_booking: the business may have been
        unpublished and the booking's services deleted since it was made (BK-26)."""
        try:
            bid = uuid_pkg.UUID(str(business_id))
        except ValueError:
            raise _not_found()
        business = (
            await self.db.execute(
                select(Business)
                .where(Business.id == bid, *(() if for_existing_booking else (Business.status == BusinessStatus.PUBLISHED,)))
                .options(
                    selectinload(Business.working_hours),
                    selectinload(Business.staff),
                    selectinload(Business.policies),
                )
            )
        ).scalar_one_or_none()
        if not business:
            raise _not_found()

        wanted = list(dict.fromkeys(service_ids))
        if not wanted and not for_existing_booking:
            raise _validation_error("serviceIds is required")
        services = (
            await self.db.execute(select(Service).where(Service.business_id == business.id, Service.id.in_(wanted)))
        ).scalars().all()
        unknown = set(wanted) - {s.id for s in services}
        if unknown:
            raise _validation_error(f"serviceIds contains services not in this business: {', '.join(map(str, unknown))}")

        mapped = await self._staff_services([s.id for s in business.staff if not s.performs_all_services])
        staff = [
            availability.StaffInfo(
                id=s.id,
                working_days=frozenset(s.working_days) if s.working_days is not None else None,
                performs_all_services=s.performs_all_services,
                service_ids=frozenset(mapped.get(s.id, ())),
            )
            for s in business.staff
        ]
        if staff_id is not None:
            member = next((s for s in staff if s.id == staff_id), None)
            if member is None:
                raise _not_found("staffId does not exist for this business")
            if not member.performs(wanted):
                raise HTTPException(
                    status_code=400,
                    detail={"error": "staff_cannot_perform", "message": "This staff member doesn't offer all of the chosen services"},
                )

        return AvailabilityContext(
            business=business,
            staff=staff,
            settings=availability.Settings(
                timezone=business.timezone,
                slot_interval_minutes=business.slot_interval_minutes,
                buffer_minutes=business.buffer_minutes,
                min_lead_minutes=business.min_lead_minutes,
                max_days_ahead=business.max_days_ahead,
            ),
            service_ids=set(wanted),
            duration_minutes=sum(s.duration_minutes for s in services),
            services={s.id: s for s in services},
        )

    async def _staff_services(self, staff_ids: list) -> dict:
        if not staff_ids:
            return {}
        rows = await self.db.execute(select(StaffService).where(StaffService.staff_id.in_(staff_ids)))
        mapping: dict = {}
        for row in rows.scalars():
            mapping.setdefault(row.staff_id, []).append(row.service_id)
        return mapping

    async def existing_bookings(
        self,
        ctx: "AvailabilityContext",
        start: datetime,
        end: datetime,
        exclude_id: Optional[uuid_pkg.UUID] = None,
    ) -> list[availability.ExistingBooking]:
        """Active bookings that could conflict with slots in [start, end), in one query.

        Widened by the buffer on both sides, since a booking's buffer can reach into the window.
        """
        margin = timedelta(minutes=ctx.settings.buffer_minutes)
        # Compare in UTC: SQLite stores the UTC wall time without an offset.
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        rows = (
            await self.db.execute(
                select(Booking)
                .where(
                    Booking.business_id == ctx.business.id,
                    Booking.status.in_(ACTIVE_STATUSES),
                    Booking.starts_at < end + margin,
                    Booking.ends_at > start - margin,
                )
                .options(selectinload(Booking.lines))
            )
        ).scalars().all()
        # Staff time off (S4) blocks that person like a booking would, on every path that checks
        # availability (slots, new bookings, reschedule, assigning, walk-ins).
        from app.modules.booking.models import StaffTimeOff

        time_off = (
            await self.db.execute(
                select(StaffTimeOff.staff_id, StaffTimeOff.starts_at, StaffTimeOff.ends_at).where(
                    StaffTimeOff.business_id == ctx.business.id,
                    StaffTimeOff.starts_at < end + margin,
                    StaffTimeOff.ends_at > start - margin,
                )
            )
        ).all()
        return [to_existing(b) for b in rows if b.id != exclude_id] + [
            availability.ExistingBooking(start=_aware(s), end=_aware(e), status="confirmed", staff_id=staff_id)
            for staff_id, s, e in time_off
        ]


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes; Postgres returns aware ones. Both are UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_existing(b: Booking) -> availability.ExistingBooking:
    return availability.ExistingBooking(
        start=_aware(b.starts_at),
        end=_aware(b.ends_at),
        status=b.status,
        staff_id=b.staff_id,
        service_ids=frozenset(line.service_id for line in b.lines if line.service_id),
        expires_at=_aware(b.expires_at) if b.expires_at else None,
    )
