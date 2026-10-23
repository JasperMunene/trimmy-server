from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    email: str
    password: str


class AdminLoginResponse(BaseModel):
    # totp: enter the code from your authenticator. enroll: first sign-in, scan secret/otpauthUrl first.
    step: Literal["totp", "enroll"]
    challenge: str
    secret: Optional[str] = None
    otpauthUrl: Optional[str] = None


class AdminVerifyRequest(BaseModel):
    challenge: str
    code: str


class AdminRefreshRequest(BaseModel):
    refresh_token: str


class AdminUser(BaseModel):
    id: str
    email: Optional[str]
    firstName: Optional[str]
    lastName: Optional[str]
    avatarUrl: Optional[str] = None
    twoStepSince: Optional[datetime] = None


class AdminTokens(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class AdminSessionResponse(BaseModel):
    user: AdminUser
    tokens: AdminTokens


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class GrantAdminRequest(ReasonRequest):
    email: str


class AuditEntry(BaseModel):
    id: str
    actorId: Optional[str]
    actorName: str
    action: str
    targetType: Optional[str]
    targetId: Optional[str]
    reason: Optional[str]
    changes: Optional[dict[str, Any]]
    ip: Optional[str]
    at: datetime


class AuditPage(BaseModel):
    entries: List[AuditEntry]
    nextCursor: Optional[str] = None


class TeamResponse(BaseModel):
    admins: List[AdminUser]


class UserStatusResponse(BaseModel):
    id: str
    disabledAt: Optional[datetime]
    disabledReason: Optional[str]
    sessionsEnded: int = 0


# ─── Look-up tools (lookup.py) ───────────────────────────────


class Money(BaseModel):
    amount: int
    currency: str = "KES"


class DayPoint(BaseModel):
    date: str  # YYYY-MM-DD, Nairobi
    signups: int = 0
    bookings: int = 0
    volume: int = 0


class FailureCount(BaseModel):
    reason: str
    count: int


class OverviewResponse(BaseModel):
    asOf: datetime
    users: dict[str, int]
    businesses: dict[str, int]
    bookings: dict[str, int]
    payments: dict[str, Any]
    refundsDue: dict[str, int]
    topFailures: List[FailureCount]
    series: List[DayPoint]


class UserRow(BaseModel):
    id: str
    name: str
    email: Optional[str]
    phone: Optional[str]
    avatarUrl: Optional[str]
    authType: str
    verified: bool
    platformRole: Optional[str]
    disabledAt: Optional[datetime]
    createdAt: datetime


class UserPage(BaseModel):
    users: List[UserRow]
    nextCursor: Optional[str] = None


class Workplace(BaseModel):
    businessId: str
    businessName: str
    businessStatus: str
    role: str  # owner | staff | front_desk
    frontDeskAccess: bool = False


class BookingRow(BaseModel):
    id: str
    reference: str
    businessId: str
    businessName: str
    customerName: str
    customerPhone: Optional[str]
    staffName: Optional[str]
    status: str
    paymentStatus: str
    source: str
    localDate: str
    localTime: str
    total: Money
    createdAt: datetime


class UserDetail(BaseModel):
    user: UserRow
    disabledReason: Optional[str]
    hasPassword: bool
    googleLinked: bool
    phoneVerifiedAt: Optional[datetime]
    twoStepSince: Optional[datetime]
    activeSessions: int
    workplaces: List[Workplace]
    bookingStats: dict[str, int]
    bookings: List[BookingRow]
    audit: List[AuditEntry]


class BusinessRow(BaseModel):
    id: str
    name: str
    status: str
    categories: List[str]
    phone: str
    address: str
    ownerId: str
    ownerName: str
    ownerEmail: Optional[str]
    ownerPhone: Optional[str]
    rating: float
    reviewCount: int
    destinationType: Optional[str]
    destinationStatus: Optional[str]
    createdAt: datetime
    publishedAt: Optional[datetime]


class BusinessPage(BaseModel):
    businesses: List[BusinessRow]
    nextCursor: Optional[str] = None


class TeamMember(BaseModel):
    staffId: str
    userId: Optional[str]
    name: str
    role: str
    frontDeskAccess: bool
    commissionPercent: Optional[int]
    email: Optional[str] = None
    phone: Optional[str] = None


class PaymentRow(BaseModel):
    id: str
    bookingId: str
    visitId: Optional[str]
    kind: str
    provider: str
    status: str
    amount: Money
    tip: Money
    phone: Optional[str]
    failureReason: Optional[str]
    mpesaReceipt: Optional[str]
    destination: Optional[str]  # where the money went: e.g. "till 123456"
    createdAt: datetime
    settledAt: Optional[datetime]


class BusinessDetail(BaseModel):
    business: BusinessRow
    description: str
    timezone: str
    teamMode: Optional[str]
    servicesCount: int
    workingHours: Optional[dict[str, Any]]
    policies: dict[str, Any]
    destination: Optional[dict[str, Any]]
    team: List[TeamMember]
    stats30d: dict[str, int]
    bookings: List[BookingRow]
    payments: List[PaymentRow]
    audit: List[AuditEntry]


class BookingLookup(BaseModel):
    bookings: List[BookingRow]
    matchedBy: Optional[str] = None  # reference | receipt | phone | id | name


class BookingEventRow(BaseModel):
    fromStatus: Optional[str]
    toStatus: str
    actorName: str
    reason: Optional[str]
    at: datetime


class BookingDetail(BaseModel):
    booking: BookingRow
    customerId: Optional[str]
    customerEmail: Optional[str]
    staffId: Optional[str]
    visitId: Optional[str]
    startsAt: datetime
    durationMinutes: int
    services: List[dict[str, Any]]
    deposit: Money
    tip: Money
    staffCommission: Optional[int]
    cancelReason: Optional[str]
    fees: dict[str, Any]
    refund: dict[str, Any]
    notes: Optional[str]
    events: List[BookingEventRow]
    payments: List[PaymentRow]
    review: Optional[dict[str, Any]]
    audit: List[AuditEntry]
