from datetime import datetime
from typing import List, Optional
import uuid as uuid_pkg

from pydantic import BaseModel, Field


class SaveCustomerRequest(BaseModel):
    # Present: update this customer. Absent: create, or update whoever already has this phone.
    customer_id: Optional[uuid_pkg.UUID] = Field(None, alias="customerId")
    name: str = Field(..., min_length=1, max_length=100)
    phone: Optional[str] = Field(None, max_length=32)
    email: Optional[str] = Field(None, max_length=254)

    model_config = {"populate_by_name": True}


class CustomerResponse(BaseModel):
    customer_id: uuid_pkg.UUID = Field(alias="customerId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    name: str
    phone: Optional[str] = None  # E.164
    email: Optional[str] = None
    # Their Trimmy profile picture, when the phone (verified) or email matches an account.
    avatar_url: Optional[str] = Field(None, alias="avatarUrl")
    created_at: datetime = Field(alias="createdAt")
    # From the business's bookings with this phone (cancelled ones excluded).
    bookings_count: int = Field(0, alias="bookingsCount")
    last_booking_at: Optional[datetime] = Field(None, alias="lastBookingAt")

    model_config = {"populate_by_name": True}


class ListCustomersResponse(BaseModel):
    customers: List[CustomerResponse]
    total: int
