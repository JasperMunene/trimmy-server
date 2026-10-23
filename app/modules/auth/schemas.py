from pydantic import BaseModel, EmailStr, Field, computed_field, model_validator
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
    # Their profile picture (a small square JPEG), or null: show initials.
    avatar_url: Optional[str] = None
    google_picture_url: Optional[str] = Field(None, exclude=True)

    @computed_field
    @property
    def has_google_picture(self) -> bool:
        """Their Google account has a picture they can use (POST /auth/me/avatar/google)."""
        return bool(self.google_picture_url)

    model_config = {"from_attributes": True}


class AuthResponse(BaseModel):
    user: UserResponse
    tokens: TokenResponse


class _EmailOrPhone(BaseModel):
    """Account recovery works with either: the email, or a phone verified with a code."""

    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=32)

    @model_validator(mode="after")
    def _one_of(self):
        if bool(self.email) == bool(self.phone):
            raise ValueError("Send either email or phone")
        return self


class ForgotPasswordRequest(_EmailOrPhone):
    pass


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6)


class ResetPasswordRequest(_EmailOrPhone):
    otp: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=8, max_length=128)


class VerifyResetOtpRequest(_EmailOrPhone):
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
