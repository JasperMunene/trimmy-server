from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime
import uuid as uuid_pkg


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    phone: Optional[str] = None


class RequestEmailOtpRequest(BaseModel):
    email: EmailStr


class CompleteRegistrationRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6)
    password: str = Field(..., min_length=8, max_length=128)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    phone: Optional[str] = None


class RequestPhoneOtpRequest(BaseModel):
    phone: str = Field(..., min_length=6, max_length=32)


class CompletePhoneRegistrationRequest(BaseModel):
    phone: str = Field(..., min_length=6, max_length=32)
    otp: str = Field(..., min_length=6, max_length=6)
    password: str = Field(..., min_length=8, max_length=128)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    email: Optional[EmailStr] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: uuid_pkg.UUID
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    auth_type: str
    verified: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AuthResponse(BaseModel):
    user: UserResponse
    tokens: TokenResponse


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6)


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=8, max_length=128)


class VerifyResetOtpRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6)


class VerifyResetOtpResponse(BaseModel):
    valid: bool


class ResendOtpRequest(BaseModel):
    email: EmailStr
    purpose: str = Field(..., pattern=r"^(verification|password_reset)$")


class MessageResponse(BaseModel):
    message: str


class LogoutRequest(BaseModel):
    refresh_token: str


class DeleteAccountRequest(BaseModel):
    password: str


class UpdateMeRequest(BaseModel):
    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(None, min_length=1, max_length=100)
    # Send null to remove the phone number; omit to leave it unchanged.
    phone: Optional[str] = Field(None, min_length=1, max_length=32)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class ChangePasswordResponse(BaseModel):
    message: str
    # All existing sessions are revoked; the caller should replace its stored tokens.
    tokens: TokenResponse


class CheckEmailRequest(BaseModel):
    email: EmailStr


class CheckEmailResponse(BaseModel):
    exists: bool


class GoogleAuthUrlResponse(BaseModel):
    url: str


class GoogleExchangeRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=256)
    # Required if a code_challenge was sent to GET /auth/google/url.
    code_verifier: Optional[str] = Field(None, min_length=43, max_length=128)
