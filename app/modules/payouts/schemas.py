from datetime import datetime
from typing import List, Optional
import uuid as uuid_pkg

from pydantic import BaseModel, Field


class RequestPayoutRequest(BaseModel):
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    # Optional: defaults to the caller's own staff record at this business (staff only ever
    # request for themselves).
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")
    amount: int = Field(..., gt=0)

    model_config = {"populate_by_name": True}


class RespondToPayoutRequest(BaseModel):
    action: str = Field(..., pattern=r"^(approve|reject)$")
    rejection_reason: Optional[str] = Field(None, alias="rejectionReason", max_length=300)

    model_config = {"populate_by_name": True}


class PayoutResponse(BaseModel):
    payout_id: uuid_pkg.UUID = Field(alias="payoutId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")
    staff_name: str = Field(alias="staffName")
    amount: int
    currency: str
    status: str  # pending | paid | rejected
    requested_at: datetime = Field(alias="requestedAt")
    responded_at: Optional[datetime] = Field(None, alias="respondedAt")
    rejection_reason: Optional[str] = Field(None, alias="rejectionReason")

    model_config = {"populate_by_name": True}


class ListPayoutsResponse(BaseModel):
    payouts: List[PayoutResponse]


class PayoutBalanceResponse(BaseModel):
    """A staff member's wallet: commission on paid bookings, less payouts pending or paid."""

    business_id: uuid_pkg.UUID = Field(alias="businessId")
    staff_id: uuid_pkg.UUID = Field(alias="staffId")
    currency: str
    earned: int  # lifetime commission on paid bookings
    pending: int  # requested, not yet answered
    paid: int  # approved payouts
    available: int  # what can be requested now: earned - pending - paid
    commission_percent: Optional[int] = Field(None, alias="commissionPercent")

    model_config = {"populate_by_name": True}
