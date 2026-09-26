from datetime import date, datetime
from typing import List
import uuid as uuid_pkg

from pydantic import BaseModel, Field


class SlotResponse(BaseModel):
    time: str = Field(examples=["09:15"])  # business local time
    starts_at: datetime = Field(alias="startsAt")  # the same moment with its UTC offset
    # Staff free for this slot. With no staffId requested, the booking is left
    # unassigned; this is who could take it.
    staff_ids: List[uuid_pkg.UUID] = Field(alias="staffIds")

    model_config = {"populate_by_name": True}


class DaySlotsResponse(BaseModel):
    date: date
    timezone: str
    duration_minutes: int = Field(alias="durationMinutes")
    slots: List[SlotResponse]

    model_config = {"populate_by_name": True}


class DayAvailabilityResponse(BaseModel):
    date: date
    is_open: bool = Field(alias="isOpen")
    has_slots: bool = Field(alias="hasSlots")

    model_config = {"populate_by_name": True}


class DaysResponse(BaseModel):
    timezone: str
    duration_minutes: int = Field(alias="durationMinutes")
    days: List[DayAvailabilityResponse]

    model_config = {"populate_by_name": True}
