from datetime import datetime
from typing import Optional
import uuid as uuid_pkg

from pydantic import BaseModel, Field


class StartPaymentRequest(BaseModel):
    # The M-Pesa number to prompt. Defaults to the phone on the booking.
    phone: Optional[str] = Field(None, examples=["0712345678"])


class ChargeRequest(BaseModel):
    method: str = Field(..., pattern=r"^(mpesa|cash)$")
    # For M-Pesa: the number to prompt. Defaults to the booking's customer phone.
    phone: Optional[str] = Field(None, examples=["0712345678"])


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

    model_config = {"populate_by_name": True}
