from datetime import datetime
from typing import List, Literal, Optional
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
    staff_avatar_url: Optional[str] = Field(None, alias="staffAvatarUrl")
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
    """A staff member's wallet at one business: commission and adjustments, less payouts."""

    business_id: uuid_pkg.UUID = Field(alias="businessId")
    staff_id: uuid_pkg.UUID = Field(alias="staffId")
    currency: str
    earned: int  # lifetime commission on paid bookings
    tips: int = 0  # adjustments, never voided
    bonuses: int = 0
    deductions: int = 0
    advances: int = 0
    pending: int  # requested, not yet answered
    paid: int  # approved payouts
    # What can be requested now: earned + tips + bonuses - deductions - advances - pending - paid.
    # Can go below 0 after a deduction or an advance.
    available: int
    commission_percent: Optional[int] = Field(None, alias="commissionPercent")

    model_config = {"populate_by_name": True}


class WalletBusinessBalance(PayoutBalanceResponse):
    business_name: str = Field(alias="businessName")
    role: str


class WalletResponse(BaseModel):
    """Every business the person works at, plus the totals across them."""

    currency: str
    earned: int
    tips: int
    bonuses: int
    deductions: int
    advances: int
    pending: int
    paid: int
    available: int
    businesses: List[WalletBusinessBalance]


AdjustmentKindLiteral = Literal["tip", "bonus", "deduction", "advance"]


class CreateAdjustmentRequest(BaseModel):
    kind: AdjustmentKindLiteral
    amount: int = Field(..., gt=0, le=10_000_000)
    note: Optional[str] = Field(None, max_length=500)
    booking_id: Optional[uuid_pkg.UUID] = Field(None, alias="bookingId")

    model_config = {"populate_by_name": True}


class AdjustmentResponse(BaseModel):
    adjustment_id: uuid_pkg.UUID = Field(alias="adjustmentId")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    staff_id: Optional[uuid_pkg.UUID] = Field(None, alias="staffId")
    staff_name: str = Field(alias="staffName")
    staff_avatar_url: Optional[str] = Field(None, alias="staffAvatarUrl")
    kind: str
    amount: int  # positive; the kind gives the direction
    signed_amount: int = Field(alias="signedAmount")
    currency: str
    note: Optional[str] = None
    booking_id: Optional[uuid_pkg.UUID] = Field(None, alias="bookingId")
    created_at: datetime = Field(alias="createdAt")
    voided_at: Optional[datetime] = Field(None, alias="voidedAt")

    model_config = {"populate_by_name": True}


class WalletEntry(BaseModel):
    entry_id: str = Field(alias="entryId")
    type: str  # commission | tip | bonus | deduction | advance | payout
    at: datetime
    amount: int  # signed effect on the balance (0 for a rejected payout)
    balance_after: int = Field(alias="balanceAfter")
    business_id: uuid_pkg.UUID = Field(alias="businessId")
    business_name: str = Field(alias="businessName")
    description: str
    booking_id: Optional[uuid_pkg.UUID] = Field(None, alias="bookingId")
    booking_reference: Optional[str] = Field(None, alias="bookingReference")
    payout_id: Optional[uuid_pkg.UUID] = Field(None, alias="payoutId")
    payout_status: Optional[str] = Field(None, alias="payoutStatus")
    payout_amount: Optional[int] = Field(None, alias="payoutAmount")
    adjustment_id: Optional[uuid_pkg.UUID] = Field(None, alias="adjustmentId")
    note: Optional[str] = None

    model_config = {"populate_by_name": True}


class WalletStatementResponse(BaseModel):
    currency: str
    balance: int  # the current available balance in this scope (= the newest entry's balanceAfter)
    entries: List[WalletEntry]  # newest first
    total: int
    next_offset: Optional[int] = Field(None, alias="nextOffset")

    model_config = {"populate_by_name": True}
