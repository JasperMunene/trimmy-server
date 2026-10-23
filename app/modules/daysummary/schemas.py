from datetime import date, datetime
from typing import List, Optional
import uuid as uuid_pkg

from pydantic import BaseModel, Field


class AmountCount(BaseModel):
    amount: int
    count: int


class Collected(BaseModel):
    """Money that came in on this day (by when it was paid, not when the visit was)."""

    cash: AmountCount
    mpesa: AmountCount
    deposits_collected: AmountCount = Field(alias="depositsCollected")
    balance_payments: AmountCount = Field(alias="balancePayments")  # checkout charges
    # Tips inside cash / mpesa above: owed to staff (already in their wallets), not the business's.
    tips: AmountCount = AmountCount(amount=0, count=0)

    model_config = {"populate_by_name": True}


class Money(BaseModel):
    amount: int
    currency: str = "KES"


class UnpaidVisit(BaseModel):
    booking_id: uuid_pkg.UUID = Field(alias="bookingId")
    customer_name: str = Field(alias="customerName")
    time: str
    status: str  # in_progress | completed
    balance_due: Money = Field(alias="balanceDue")

    model_config = {"populate_by_name": True}


class Submission(BaseModel):
    submitted_at: Optional[datetime] = Field(None, alias="submittedAt")
    submitted_by: Optional[str] = Field(None, alias="submittedBy")  # name
    notes: Optional[str] = None

    model_config = {"populate_by_name": True}


class DaySummaryResponse(BaseModel):
    date: date
    timezone: str
    appointments: int  # booked ahead (online or on the customer's behalf), not cancelled
    walk_ins: int = Field(alias="walkIns")
    completed: int
    no_shows: int = Field(alias="noShows")
    cancelled: int
    collected: Collected
    refunds_paid: AmountCount = Field(alias="refundsPaid")
    unpaid: List[UnpaidVisit]
    submission: Submission

    model_config = {"populate_by_name": True}


class SubmitDayRequest(BaseModel):
    notes: Optional[str] = Field(None, max_length=1000, examples=["KSh 200 short due to change"])
