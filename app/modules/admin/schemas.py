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
