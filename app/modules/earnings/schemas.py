from datetime import date, datetime
from typing import List, Optional
import uuid as uuid_pkg

from pydantic import BaseModel, Field


class BucketResponse(BaseModel):
    label: str  # "9 AM" | "Mon" | "Oct 5" | "Oct", as the app labels them
    amount: int
    start: datetime
    end: datetime
    bookings_count: int = Field(alias="bookingsCount")

    model_config = {"populate_by_name": True}


class PaymentMethodSlice(BaseModel):
    method: str  # mpesa | cash
    amount: int
    count: int


class ServiceRow(BaseModel):
    service_id: Optional[uuid_pkg.UUID] = Field(alias="serviceId")  # null for a service since deleted
    name: str
    amount: int
    bookings_count: int = Field(alias="bookingsCount")

    model_config = {"populate_by_name": True}


class TransactionRow(BaseModel):
    booking_id: uuid_pkg.UUID = Field(alias="bookingId")
    service_id: Optional[uuid_pkg.UUID] = Field(alias="serviceId")
    date: str
    time: str
    customer_name: str = Field(alias="customerName")
    service_name: str = Field(alias="serviceName")
    amount: int
    payment_method: Optional[str] = Field(alias="paymentMethod")

    model_config = {"populate_by_name": True}


class StaffRow(BaseModel):
    staff_id: Optional[uuid_pkg.UUID] = Field(alias="staffId")  # null = unassigned bookings
    name: str
    amount: int
    bookings_count: int = Field(alias="bookingsCount")
    commission: int

    model_config = {"populate_by_name": True}


class CollectedRow(BaseModel):
    method: str  # mpesa | cash
    kind: str  # deposit | charge
    amount: int
    count: int


class MoneyResponse(BaseModel):
    """Money outside paid sales, for bookings in the range."""

    deposits_collected: int = Field(alias="depositsCollected")
    cancellation_fees: int = Field(alias="cancellationFees")  # kept from cancelled bookings' deposits
    refunds_due: int = Field(alias="refundsDue")  # the business still owes these back
    refunds_paid: int = Field(alias="refundsPaid")
    collected: List[CollectedRow]  # every successful payment, by method and kind

    model_config = {"populate_by_name": True}


class PreviousPeriod(BaseModel):
    """The same-length period just before `from`, for "vs last week" comparisons."""

    from_: date = Field(alias="from")
    to: date
    total_revenue: int = Field(alias="totalRevenue")
    completed: int
    avg_ticket: int = Field(alias="avgTicket")
    revenue_change_percent: Optional[float] = Field(alias="revenueChangePercent")  # null when the previous period had none

    model_config = {"populate_by_name": True}


class EarningsSummaryResponse(BaseModel):
    # The contract (fulfillment.json#get-earnings-summary)
    total_revenue: int = Field(alias="totalRevenue")
    completed: int
    no_show: int = Field(alias="noShow")
    cancelled: int
    avg_ticket: int = Field(alias="avgTicket")
    buckets: List[BucketResponse]
    payment_methods: List[PaymentMethodSlice] = Field(alias="paymentMethods")
    services: List[ServiceRow]
    transactions: List[TransactionRow]
    # Extras
    currency: str
    from_: date = Field(alias="from")
    to: date
    timezone: str
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")  # set when scoped to one person
    granularity: str  # hour | day | week | month
    paid_bookings: int = Field(alias="paidBookings")
    commission: int  # the scoped person's commission, or everyone's on the business view
    transactions_total: int = Field(alias="transactionsTotal")  # before transactionsLimit
    staff: List[StaffRow]  # per person; empty when scoped to one staff member
    money: MoneyResponse
    previous: PreviousPeriod

    model_config = {"populate_by_name": True}
