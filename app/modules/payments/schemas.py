from datetime import datetime
from typing import List, Optional
import uuid as uuid_pkg

from pydantic import BaseModel, Field


class StartPaymentRequest(BaseModel):
    # The M-Pesa number to prompt. Defaults to the phone on the booking.
    phone: Optional[str] = Field(None, examples=["0712345678"])


class ChargeRequest(BaseModel):
    method: str = Field(..., pattern=r"^(mpesa|cash)$")
    # For M-Pesa: the number to prompt. Defaults to the booking's customer phone.
    phone: Optional[str] = Field(None, examples=["0712345678"])
    # A tip for the booking's staff member, charged together with the balance. It goes to their
    # wallet once the payment succeeds, not to the business's revenue.
    tip: int = Field(0, ge=0, le=100_000)


class VisitTip(BaseModel):
    staff_id: uuid_pkg.UUID = Field(alias="staffId")
    amount: int = Field(..., ge=0, le=100_000)

    model_config = {"populate_by_name": True}


class VisitChargeRequest(BaseModel):
    method: str = Field(..., pattern=r"^(mpesa|cash)$")
    phone: Optional[str] = Field(None, examples=["0712345678"])
    # The customer's tip, already split (evenly, by service, or their own amounts): one entry per
    # person, each going to that person's wallet. Not revenue.
    tips: List[VisitTip] = Field(default_factory=list, max_length=20)
    # Charge just these parts (the rest later); default: everything unpaid, if it's all started or done.
    booking_ids: Optional[List[uuid_pkg.UUID]] = Field(None, alias="bookingIds")

    model_config = {"populate_by_name": True}


class StartPaymentResponse(BaseModel):
    payment_id: uuid_pkg.UUID = Field(alias="paymentId")
    booking_id: uuid_pkg.UUID = Field(alias="bookingId")
    status: str  # pending: the customer has an M-Pesa prompt on their phone
    amount: int
    currency: str
    phone: str
    # What M-Pesa said, e.g. "Success. Request accepted for processing".
    customer_message: str = Field(alias="customerMessage")
    # Pay before this, or the booking's slot is released.
    expires_at: Optional[datetime] = Field(None, alias="expiresAt")
    # Set for a visit checkout: poll GET /visits/{visitId}.
    visit_id: Optional[uuid_pkg.UUID] = Field(None, alias="visitId")

    model_config = {"populate_by_name": True}
