from datetime import date, datetime
from typing import List, Optional
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


# ─── Bookings ────────────────────────────────────────────────


class Money(BaseModel):
    amount: int
    currency: str = "KES"


class BookingServiceRequest(BaseModel):
    service_id: uuid_pkg.UUID = Field(alias="serviceId")
    quantity: int = Field(1, ge=1, le=10)

    model_config = {"populate_by_name": True}


class CreateBookingRequest(BaseModel):
    """Prices, names, totals and the deposit all come from the server (D7)."""

    business_id: uuid_pkg.UUID = Field(alias="businessId")
    # null = "any available": the booking is left unassigned for the owner (D4).
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")
    services: List[BookingServiceRequest] = Field(..., min_length=1, max_length=10)
    date: date
    time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["14:30"])
    notes: Optional[str] = Field(None, max_length=500)
    # The total the customer saw. If the server's total differs, 409 price_changed.
    expected_total: Optional[Money] = Field(None, alias="expectedTotal")

    model_config = {"populate_by_name": True}


class AssignStaffRequest(BaseModel):
    staff_id: uuid_pkg.UUID = Field(alias="staffId")

    model_config = {"populate_by_name": True}


class BookingLineResponse(BaseModel):
    service_id: Optional[uuid_pkg.UUID] = Field(alias="serviceId")  # null if the service was deleted
    name: str
    duration_minutes: int = Field(alias="durationMinutes")
    price: Money
    quantity: int

    model_config = {"populate_by_name": True}


class DepositPaymentResponse(BaseModel):
    """The latest attempt to pay this booking's deposit (from BK-31)."""

    payment_id: uuid_pkg.UUID = Field(alias="paymentId")
    status: str  # pending | succeeded | failed | timeout
    failure_reason: Optional[str] = Field(None, alias="failureReason")  # cancelled | insufficient_funds | wrong_pin | timeout | busy | failed

    model_config = {"populate_by_name": True}


class BookingResponse(BaseModel):
    """The app's Booking type (mobile src/types/booking.ts), plus reference, startsAt, expiresAt."""

    booking_id: uuid_pkg.UUID = Field(alias="bookingId")
    reference: str
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    business_name: str = Field(alias="businessName")
    customer_name: str = Field(alias="customerName")
    customer_phone: Optional[str] = Field(None, alias="customerPhone")
    customer_email: Optional[str] = Field(None, alias="customerEmail")
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")
    staff_name: str = Field(alias="staffName")  # "Any available" while unassigned
    services: List[BookingLineResponse]
    date: date
    time: str
    starts_at: datetime = Field(alias="startsAt")
    duration_minutes: int = Field(alias="durationMinutes")
    total_amount: Money = Field(alias="totalAmount")
    deposit_amount: Optional[Money] = Field(None, alias="depositAmount")
    status: str
    source: str
    payment_status: str = Field(alias="paymentStatus")  # unpaid | deposit_paid | paid
    payment_method: Optional[str] = Field(None, alias="paymentMethod")
    payment_reference: Optional[str] = Field(None, alias="paymentReference")
    # Set while status is pending_payment: pay the deposit before this or the slot is released.
    expires_at: Optional[datetime] = Field(None, alias="expiresAt")
    cancel_reason: Optional[str] = Field(None, alias="cancelReason")
    # Null until a deposit payment has been started (BK-31).
    deposit_payment: Optional[DepositPaymentResponse] = Field(None, alias="depositPayment")
    notes: Optional[str] = None
    created_at: datetime = Field(alias="createdAt")

    model_config = {"populate_by_name": True}


class ListBookingsResponse(BaseModel):
    bookings: List[BookingResponse]
    # Pass as ?cursor= to get the next page; null on the last page.
    next_cursor: Optional[str] = Field(None, alias="nextCursor")

    model_config = {"populate_by_name": True}
