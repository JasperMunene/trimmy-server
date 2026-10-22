"""Staff time off (S4): blocks of time someone can't be booked.

    GET    /businesses/{businessId}/time-off?from=&to=[&staffId=]   owner / front desk: everyone; staff: their own
    POST   /businesses/{businessId}/staff/{staffId}/time-off        the person themselves, the owner or front desk
    DELETE /time-off/{timeOffId}                                     same people

Times are the business's local wall time. Availability (service.py existing_bookings) treats each
block like a booking for that person, so it's never offered and never booked over.
"""
import uuid as uuid_pkg
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.auth.models import User
from app.modules.booking.business_bookings import FRONT_DESK, OWNER, BusinessBookingsService
from app.modules.booking.models import ACTIVE_STATUSES, Booking, StaffTimeOff
from app.modules.business.models import Business, Staff

router = APIRouter(tags=["Bookings"])

MAX_RANGE_DAYS = 62
MAX_BLOCK_DAYS = 31


def _error(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"error": error, "message": message})


class CreateTimeOffRequest(BaseModel):
    date: date
    # Omit both (or send allDay) for the whole day. Business-local HH:MM.
    start: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")
    all_day: bool = Field(False, alias="allDay")
    # For more than one day off in a row (inclusive); all-day only.
    until: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=100)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _shape(self):
        if self.start is None and self.end is None:
            self.all_day = True
        if not self.all_day and (self.start is None or self.end is None):
            raise ValueError("start and end are both needed, or allDay")
        if not self.all_day and self.start >= self.end:
            raise ValueError("end must be after start")
        if self.until is not None:
            if not self.all_day:
                raise ValueError("until is for whole days off")
            if self.until < self.date or (self.until - self.date).days >= MAX_BLOCK_DAYS:
                raise ValueError(f"until must be on or after date, and within {MAX_BLOCK_DAYS} days")
        return self


class TimeOffResponse(BaseModel):
    time_off_id: uuid_pkg.UUID = Field(alias="timeOffId")
    staff_id: uuid_pkg.UUID = Field(alias="staffId")
    staff_name: str = Field(alias="staffName")
    date: date
    until: date  # the last day (same as date unless it spans days)
    start: str  # business-local HH:MM
    end: str  # "24:00" for the end of the day
    all_day: bool = Field(alias="allDay")
    reason: Optional[str] = None

    model_config = {"populate_by_name": True}


class ListTimeOffResponse(BaseModel):
    time_off: List[TimeOffResponse] = Field(alias="timeOff")

    model_config = {"populate_by_name": True}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _response(row: StaffTimeOff, staff_name: str, tz: ZoneInfo) -> TimeOffResponse:
    starts, ends = _aware(row.starts_at).astimezone(tz), _aware(row.ends_at).astimezone(tz)
    last = ends - timedelta(microseconds=1)  # an all-day block ends at the next midnight
    end_label = "24:00" if ends.time() == time(0) and ends.date() > starts.date() else ends.strftime("%H:%M")
    return TimeOffResponse(
        timeOffId=row.id, staffId=row.staff_id, staffName=staff_name, date=starts.date(), until=last.date(),
        start=starts.strftime("%H:%M"), end=end_label, allDay=row.all_day, reason=row.reason,
    )


class TimeOffService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.roles = BusinessBookingsService(db)

    async def _business(self, business_id: str) -> Business:
        try:
            business = await self.db.get(Business, uuid_pkg.UUID(str(business_id)))
        except ValueError:
            business = None
        if business is None:
            raise _error(404, "not_found", "No business found for this id")
        return business

    async def _may_manage(self, user: User, business: Business, staff_id: uuid_pkg.UUID) -> None:
        role, me = await self.roles._role(user, business.id)
        if role is None:
            raise _error(404, "not_found", "No business found for this id")
        if role not in (OWNER, FRONT_DESK) and (me is None or me.id != staff_id):
            raise _error(403, "forbidden", "You can only change your own time off")

    async def list(self, user: User, business_id: str, start: date, end: date,
                   staff_id: Optional[uuid_pkg.UUID]) -> ListTimeOffResponse:
        business = await self._business(business_id)
        role, me = await self.roles._role(user, business.id)
        if role is None:
            raise _error(404, "not_found", "No business found for this id")
        if end < start or (end - start).days >= MAX_RANGE_DAYS:
            raise _error(400, "validation_error", f"to must be on or after from, and within {MAX_RANGE_DAYS} days")
        if role not in (OWNER, FRONT_DESK):
            staff_id = me.id  # staff see their own
        tz = ZoneInfo(business.timezone)
        window_start = datetime.combine(start, time(0), tz).astimezone(timezone.utc)
        window_end = datetime.combine(end + timedelta(days=1), time(0), tz).astimezone(timezone.utc)
        query = (
            select(StaffTimeOff, Staff.name)
            .join(Staff, Staff.id == StaffTimeOff.staff_id)
            .where(StaffTimeOff.business_id == business.id, StaffTimeOff.starts_at < window_end,
                   StaffTimeOff.ends_at > window_start)
            .order_by(StaffTimeOff.starts_at)
        )
        if staff_id:
            query = query.where(StaffTimeOff.staff_id == staff_id)
        rows = (await self.db.execute(query)).all()
        return ListTimeOffResponse(timeOff=[_response(row, name, tz) for row, name in rows])

    async def create(self, user: User, business_id: str, staff_id: str, data: CreateTimeOffRequest) -> TimeOffResponse:
        business = await self._business(business_id)
        try:
            sid = uuid_pkg.UUID(str(staff_id))
        except ValueError:
            raise _error(404, "not_found", "No staff member found for this id")
        staff = await self.db.get(Staff, sid)
        if staff is None or staff.business_id != business.id:
            raise _error(404, "not_found", "No staff member found for this id")
        await self._may_manage(user, business, sid)

        tz = ZoneInfo(business.timezone)
        if data.all_day:
            starts = datetime.combine(data.date, time(0), tz)
            ends = datetime.combine((data.until or data.date) + timedelta(days=1), time(0), tz)
        else:
            starts = datetime.combine(data.date, time.fromisoformat(data.start), tz)
            ends = (datetime.combine(data.date + timedelta(days=1), time(0), tz) if data.end == "24:00"
                    else datetime.combine(data.date, time.fromisoformat(data.end), tz))
        if ends <= datetime.now(timezone.utc):
            raise _error(400, "validation_error", "That time has already passed")

        # Their bookings then would be left without anyone: move or reassign them first.
        clashes = (await self.db.execute(
            select(Booking.reference).where(
                Booking.staff_id == sid, Booking.status.in_(ACTIVE_STATUSES),
                Booking.starts_at < ends.astimezone(timezone.utc), Booking.ends_at > starts.astimezone(timezone.utc),
            ).order_by(Booking.starts_at)
        )).scalars().all()
        if clashes:
            raise HTTPException(status_code=409, detail={
                "error": "has_bookings",
                "message": f"{staff.name} has {len(clashes)} booking{'s' if len(clashes) > 1 else ''} then. "
                           "Move or reassign them first.",
                "references": list(clashes),
            })

        row = StaffTimeOff(business_id=business.id, staff_id=sid, starts_at=starts.astimezone(timezone.utc),
                           ends_at=ends.astimezone(timezone.utc), all_day=data.all_day,
                           reason=(data.reason or "").strip() or None, created_by=user.id)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return _response(row, staff.name, tz)

    async def delete(self, user: User, time_off_id: str) -> None:
        try:
            row = await self.db.get(StaffTimeOff, uuid_pkg.UUID(str(time_off_id)))
        except ValueError:
            row = None
        if row is None:
            raise _error(404, "not_found", "No time off found for this id")
        business = await self._business(str(row.business_id))
        await self._may_manage(user, business, row.staff_id)
        await self.db.delete(row)
        await self.db.commit()


@router.get("/businesses/{business_id}/time-off", response_model=ListTimeOffResponse)
async def list_time_off(
    business_id: str,
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    staff_id: Optional[uuid_pkg.UUID] = Query(None, alias="staffId"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await TimeOffService(db).list(current_user, business_id, from_, to, staff_id)


@router.post("/businesses/{business_id}/staff/{staff_id}/time-off", status_code=201, response_model=TimeOffResponse)
async def create_time_off(
    business_id: str,
    staff_id: str,
    data: CreateTimeOffRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await TimeOffService(db).create(current_user, business_id, staff_id, data)


@router.delete("/time-off/{time_off_id}", status_code=204)
async def delete_time_off(time_off_id: str, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await TimeOffService(db).delete(current_user, time_off_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
