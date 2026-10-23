from datetime import date, datetime
from typing import List, Literal, Optional
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


class PaymentDestinationResponse(BaseModel):
    """Where an M-Pesa payment was routed (D1: straight to the business). Account numbers are
    shortened to their last 4 characters."""

    type: str  # mpesa_till | mpesa_paybill | bank_account
    label: str  # e.g. "Till 123456", "Equity Bank Kenya, account …6789"


class DepositPaymentResponse(BaseModel):
    """The latest attempt to pay this booking's deposit (from BK-31), or its checkout charge."""

    payment_id: uuid_pkg.UUID = Field(alias="paymentId")
    status: str  # pending | succeeded | failed | timeout
    failure_reason: Optional[str] = Field(None, alias="failureReason")  # cancelled | insufficient_funds | wrong_pin | timeout | busy | failed
    # BK-34: what was asked for, and once it succeeded, the M-Pesa receipt and where it went.
    amount: Optional[Money] = None
    receipt: Optional[str] = None
    paid_at: Optional[datetime] = Field(None, alias="paidAt")
    destination: Optional[PaymentDestinationResponse] = None

    model_config = {"populate_by_name": True}


class NoShowFeeResponse(BaseModel):
    """The no-show fee (BK-64). Only the deposit can be kept; the rest is informational."""

    percent: int  # the business's no-show fee percent when it was applied
    fee: Money  # the whole fee: percent of the booking total
    kept: Money  # taken from the paid deposit
    uncollected: Money  # the part the deposit didn't cover

    model_config = {"populate_by_name": True}


class BookingReviewResponse(BaseModel):
    """booking.json#create-review response, and Booking.review once written."""

    review_id: uuid_pkg.UUID = Field(alias="reviewId")
    author_name: str = Field(alias="authorName")
    rating: int
    text: Optional[str] = None
    created_at: datetime = Field(alias="createdAt")

    model_config = {"populate_by_name": True}


class RefundResponse(BaseModel):
    """Money the business owes back after a cancellation (D10: refunded by hand)."""

    amount: Money
    status: str  # due | refunded
    refunded_at: Optional[datetime] = Field(None, alias="refundedAt")
    reference: Optional[str] = None  # e.g. the M-Pesa code of the refund

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
    # Linked to the customer's Trimmy account: they see it in their app and get its notifications.
    customer_has_account: bool = Field(False, alias="customerHasAccount")
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")
    staff_name: str = Field(alias="staffName")  # "Any available" while unassigned
    staff_avatar_url: Optional[str] = Field(None, alias="staffAvatarUrl")
    # The customer's profile picture, when the booking is linked to their account.
    customer_avatar_url: Optional[str] = Field(None, alias="customerAvatarUrl")
    services: List[BookingLineResponse]
    date: date
    time: str
    starts_at: datetime = Field(alias="startsAt")
    duration_minutes: int = Field(alias="durationMinutes")
    total_amount: Money = Field(alias="totalAmount")
    deposit_amount: Optional[Money] = Field(None, alias="depositAmount")
    # BK-34: the deposit actually paid (null if none was, or it hasn't been yet).
    deposit_paid: Optional[Money] = Field(None, alias="depositPaid")
    # A tip paid with the checkout charge (for the staff member, through their wallet).
    tip: Optional[Money] = None
    status: str
    source: str
    # Set when this booking is one part of a multi-staff visit (GET /visits/{visitId}).
    visit_id: Optional[uuid_pkg.UUID] = Field(None, alias="visitId")
    payment_status: str = Field(alias="paymentStatus")  # unpaid | deposit_paid | paid
    payment_method: Optional[str] = Field(None, alias="paymentMethod")
    payment_reference: Optional[str] = Field(None, alias="paymentReference")
    # Set while status is pending_payment: pay the deposit before this or the slot is released.
    expires_at: Optional[datetime] = Field(None, alias="expiresAt")
    cancel_reason: Optional[str] = Field(None, alias="cancelReason")
    # Null until a deposit payment has been started (BK-31).
    deposit_payment: Optional[DepositPaymentResponse] = Field(None, alias="depositPayment")
    # What charge-booking would take now: the total minus any deposit paid (0 once paid).
    balance_due: Optional[Money] = Field(None, alias="balanceDue")
    # The latest checkout charge (null until one is started); same shape as depositPayment.
    charge_payment: Optional[DepositPaymentResponse] = Field(None, alias="chargePayment")
    # Set when a cancellation kept part of the deposit, or left a refund owed (BK-60).
    cancellation_fee: Optional[Money] = Field(None, alias="cancellationFee")
    # The customer's review of this visit; null until written (offer it once status is completed).
    review: Optional[BookingReviewResponse] = None
    # Set once the business marks the booking a no-show.
    no_show_fee: Optional[NoShowFeeResponse] = Field(None, alias="noShowFee")
    refund: Optional[RefundResponse] = None
    notes: Optional[str] = None
    created_at: datetime = Field(alias="createdAt")

    model_config = {"populate_by_name": True}


class ListBookingsResponse(BaseModel):
    bookings: List[BookingResponse]
    # Pass as ?cursor= to get the next page; null on the last page.
    next_cursor: Optional[str] = Field(None, alias="nextCursor")

    model_config = {"populate_by_name": True}


# ─── Business-side bookings (BK-63) ──────────────────────────

class OwnerBookingRequest(BaseModel):
    """A booking the business records itself (walk-in or on a customer's behalf). Prices come from the server."""

    business_id: uuid_pkg.UUID = Field(alias="businessId")
    customer_name: Optional[str] = Field(None, alias="customerName", max_length=100)  # default "Walk-in customer"
    customer_phone: Optional[str] = Field(None, alias="customerPhone", max_length=32)
    customer_email: Optional[str] = Field(None, alias="customerEmail", max_length=254)
    services: List[BookingServiceRequest] = Field(..., min_length=1, max_length=10)
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")  # null = unassigned
    notes: Optional[str] = Field(None, max_length=500)

    model_config = {"populate_by_name": True}


class ScheduledBookingRequest(OwnerBookingRequest):
    date: date
    time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["14:00"])


# ─── Visits: one customer, several staff ─────────────────────

class AddServiceRequest(BaseModel):
    """Add a service to a customer's visit, done by someone (else): hair, then nails."""

    services: List[BookingServiceRequest] = Field(..., min_length=1, max_length=10)
    staff_id: uuid_pkg.UUID = Field(alias="staffId")
    # now = starts straight away (alongside what they're having done); after = when the booking it's
    # added to ends; at = a time today.
    start: Literal["now", "after", "at"] = "now"
    time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["14:30"])
    notes: Optional[str] = Field(None, max_length=500)

    model_config = {"populate_by_name": True}


class VisitStaffResponse(BaseModel):
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")
    staff_name: str = Field(alias="staffName")
    staff_avatar_url: Optional[str] = Field(None, alias="staffAvatarUrl")
    subtotal: Money
    balance_due: Money = Field(alias="balanceDue")
    booking_ids: List[uuid_pkg.UUID] = Field(alias="bookingIds")

    model_config = {"populate_by_name": True}


class VisitNotReadyResponse(BaseModel):
    booking_id: uuid_pkg.UUID = Field(alias="bookingId")
    status: str
    staff_name: str = Field(alias="staffName")

    model_config = {"populate_by_name": True}


class VisitResponse(BaseModel):
    """A customer's visit across several bookings (and staff), for checking out together."""

    visit_id: uuid_pkg.UUID = Field(alias="visitId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    customer_name: str = Field(alias="customerName")
    customer_phone: Optional[str] = Field(None, alias="customerPhone")
    # Soonest first. Cancelled and no-show bookings are listed but not counted in the totals.
    bookings: List[BookingResponse]
    # One row per person, with what their part of the visit comes to (for splitting a tip).
    staff: List[VisitStaffResponse]
    total: Money
    deposit_paid: Money = Field(alias="depositPaid")
    balance_due: Money = Field(alias="balanceDue")
    payment_status: Literal["unpaid", "partially_paid", "paid"] = Field(alias="paymentStatus")
    # Unpaid and started or done: what POST /visits/{id}/charge would take now.
    chargeable_booking_ids: List[uuid_pkg.UUID] = Field(alias="chargeableBookingIds")
    # Still to start: start them, cancel them, or charge the rest without them.
    not_ready: List[VisitNotReadyResponse] = Field(alias="notReady")
    # The latest visit checkout charge (null until one is started).
    charge_payment: Optional[DepositPaymentResponse] = Field(None, alias="chargePayment")

    model_config = {"populate_by_name": True}


class UpdateBookingStatusRequest(BaseModel):
    # in_progress = check in / start; completed; no_show; cancelled
    status: str = Field(..., pattern=r"^(in_progress|completed|no_show|cancelled)$")
    reason: Optional[str] = Field(None, max_length=100)


class BusinessBookingsResponse(BaseModel):
    bookings: List[BookingResponse]



# ─── Cancelling (BK-60) ──────────────────────────────────────

class CancelPreviewResponse(BaseModel):
    booking_id: uuid_pkg.UUID = Field(alias="bookingId")
    can_cancel: bool = Field(alias="canCancel")
    reason_blocked: Optional[str] = Field(None, alias="reasonBlocked")
    # Cancel before this for a full refund.
    free_until: datetime = Field(alias="freeUntil")
    within_free_window: bool = Field(alias="withinFreeWindow")
    deposit_paid: Money = Field(alias="depositPaid")
    fee: Money  # kept by the business
    refund: Money  # owed back; the business refunds it by hand
    late_fee_percent: int = Field(alias="lateFeePercent")
    message: str

    model_config = {"populate_by_name": True}


class CancelBookingRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=100)  # optional note from the reason picker


class MarkRefundedRequest(BaseModel):
    reference: Optional[str] = Field(None, max_length=50, examples=["SJK4R2T8QX"])


# ─── Rescheduling (BK-61) ────────────────────────────────────

class RescheduleRequest(BaseModel):
    date: date
    time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["15:30"])
    # Optional: move to someone else too. Omit to keep the current staff (or unassigned).
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")

    model_config = {"populate_by_name": True}



class CreateReviewRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    text: Optional[str] = Field(None, max_length=1000)
