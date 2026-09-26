import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.booking import availability
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
class _Context:
    business: Business
    staff: list[availability.StaffInfo]
    settings: availability.Settings
    service_ids: set[uuid_pkg.UUID]
    duration_minutes: int


class AvailabilityService:
    def __init__(self, db: AsyncSession, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.now(timezone.utc)

    async def day(
        self, business_id: str, day: date, service_ids: list[uuid_pkg.UUID], staff_id: Optional[uuid_pkg.UUID]
    ) -> DaySlotsResponse:
        ctx = await self._context(business_id, service_ids, staff_id)
        tz = ZoneInfo(ctx.settings.timezone)
        start = datetime.combine(day, datetime.min.time(), tz)
        slots = availability.day_slots(
            day=day,
            weekly_hours=ctx.business.working_hours.weekly_hours if ctx.business.working_hours else None,
            staff=ctx.staff,
            bookings=await self._existing_bookings(ctx.business.id, start, start.replace(hour=23, minute=59)),
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
        ctx = await self._context(business_id, service_ids, staff_id)
        tz = ZoneInfo(ctx.settings.timezone)
        start_day = start_day or self.now.astimezone(tz).date()
        start = datetime.combine(start_day, datetime.min.time(), tz)
        end = datetime.combine(date.fromordinal(start_day.toordinal() + days), datetime.min.time(), tz)
        result = availability.open_days(
            start_day=start_day,
            days=days,
            weekly_hours=ctx.business.working_hours.weekly_hours if ctx.business.working_hours else None,
            staff=ctx.staff,
            bookings=await self._existing_bookings(ctx.business.id, start, end),
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

    async def _context(
        self, business_id: str, service_ids: list[uuid_pkg.UUID], staff_id: Optional[uuid_pkg.UUID]
    ) -> _Context:
        try:
            bid = uuid_pkg.UUID(str(business_id))
        except ValueError:
            raise _not_found()
        business = (
            await self.db.execute(
                select(Business)
                .where(Business.id == bid, Business.status == BusinessStatus.PUBLISHED)
                .options(selectinload(Business.working_hours), selectinload(Business.staff))
            )
        ).scalar_one_or_none()
        if not business:
            raise _not_found()

        wanted = list(dict.fromkeys(service_ids))
        if not wanted:
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

        return _Context(
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
        )

    async def _staff_services(self, staff_ids: list) -> dict:
        if not staff_ids:
            return {}
        rows = await self.db.execute(select(StaffService).where(StaffService.staff_id.in_(staff_ids)))
        mapping: dict = {}
        for row in rows.scalars():
            mapping.setdefault(row.staff_id, []).append(row.service_id)
        return mapping

    async def _existing_bookings(
        self, business_id: uuid_pkg.UUID, start: datetime, end: datetime
    ) -> list[availability.ExistingBooking]:
        """Bookings that could conflict in [start, end), in one query.

        There is no bookings table until BK-20; this is where it plugs in.
        """
        return []
