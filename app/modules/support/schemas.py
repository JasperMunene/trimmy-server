from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

Category = Literal["booking", "payment", "refund", "account", "business", "bug", "other"]
Status = Literal["new", "open", "waiting", "resolved", "closed"]
Priority = Literal["low", "normal", "high", "urgent"]


class CreateTicketRequest(BaseModel):
    category: Category
    message: str = Field(min_length=1, max_length=5000)
    subject: Optional[str] = Field(None, max_length=140)
    bookingId: Optional[str] = None
    businessId: Optional[str] = None


class MessageOut(BaseModel):
    id: str
    kind: str  # customer | agent | note | event
    authorName: str
    body: str
    attachmentUrl: Optional[str]
    channel: str
    createdAt: datetime


class TicketRow(BaseModel):
    id: str
    number: str
    subject: str
    category: str
    status: str
    priority: str
    channel: str
    requesterId: Optional[str]
    requesterName: str
    businessId: Optional[str]
    bookingId: Optional[str]
    assigneeId: Optional[str]
    assigneeName: Optional[str] = None
    createdAt: datetime
    lastMessageAt: datetime
    lastCustomerMessageAt: Optional[datetime]
    firstResponseAt: Optional[datetime]
    preview: str
    lastFrom: Optional[str]  # who wrote last: customer | agent | note


class MyTicketsResponse(BaseModel):
    tickets: List[TicketRow]


class TicketResponse(BaseModel):
    ticket: TicketRow
    bookingReference: Optional[str] = None
    businessName: Optional[str] = None
    messages: List[MessageOut]


# ─── Team (admin.py) ─────────────────────────────────────────


class AdminTicketPage(BaseModel):
    tickets: List[TicketRow]
    nextCursor: Optional[str] = None


class SupportSummary(BaseModel):
    unassigned: int
    mine: int
    open: int
    waiting: int
    oldestWaitingSince: Optional[datetime]


class UpdateTicketRequest(BaseModel):
    status: Optional[Status] = None
    priority: Optional[Priority] = None
    category: Optional[Category] = None
    # An admin's user id, "me", or "" to unassign.
    assigneeId: Optional[str] = None
    bookingId: Optional[str] = None
    businessId: Optional[str] = None


class AdminTicketDetail(BaseModel):
    ticket: TicketRow
    messages: List[MessageOut]
    requester: Optional[Any] = None   # admin.schemas.UserRow
    booking: Optional[Any] = None     # admin.schemas.BookingRow
    business: Optional[Any] = None    # admin.schemas.BusinessRow
    recentBookings: List[Any] = []
    otherTickets: List[TicketRow] = []


class MacroIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=5000)


class MacroOut(MacroIn):
    id: str
    updatedAt: datetime


class MacrosResponse(BaseModel):
    macros: List[MacroOut]
