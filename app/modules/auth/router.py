import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.rate_limit import limiter
from app.modules.auth.models import User
from app.modules.auth.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    CheckEmailRequest,
    CheckEmailResponse,
    CompletePhoneRegistrationRequest,
    CompleteRegistrationRequest,
    DeleteAccountRequest,
    GoogleAuthUrlResponse,
    GoogleExchangeRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    RequestEmailOtpRequest,
    RequestPhoneOtpRequest,
    ResendOtpRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateMeRequest,
    UserResponse,
    VerifyEmailRequest,
    VerifyResetOtpRequest,
    VerifyResetOtpResponse,
)
from app.modules.auth import google
from app.modules.auth.google import GoogleAuthError, GoogleAuthService
from app.modules.auth.service import AuthService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    return await service.register(data)


@router.post("/request-email-otp", response_model=MessageResponse)
@limiter.limit(settings.RATE_LIMIT_CHECK_EMAIL)
async def request_email_otp(
    request: Request, data: RequestEmailOtpRequest, db: AsyncSession = Depends(get_db)
):
    service = AuthService(db)
    return await service.request_email_otp(data.email)


@router.post("/complete-registration", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def complete_registration(
    data: CompleteRegistrationRequest, db: AsyncSession = Depends(get_db)
):
    service = AuthService(db)
    return await service.complete_registration(data)


@router.post("/request-phone-otp", response_model=MessageResponse)
@limiter.limit(settings.RATE_LIMIT_CHECK_EMAIL)
async def request_phone_otp(
    request: Request, data: RequestPhoneOtpRequest, db: AsyncSession = Depends(get_db)
):
    service = AuthService(db)
    return await service.request_phone_otp(data.phone)


@router.post("/complete-phone-registration", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def complete_phone_registration(
    data: CompletePhoneRegistrationRequest, db: AsyncSession = Depends(get_db)
):
    service = AuthService(db)
    return await service.complete_phone_registration(data)



@router.post("/login", response_model=AuthResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    return await service.login(data)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    return await service.refresh(data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    data: LogoutRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = AuthService(db)
    await service.logout(current_user, data.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/verify-email", response_model=AuthResponse)
async def verify_email(data: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    return await service.verify_email(data)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserResponse)
async def update_me(
    data: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = AuthService(db)
    return await service.update_me(current_user, data)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    data: DeleteAccountRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = AuthService(db)
    await service.delete_account(current_user, data.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-password", response_model=ChangePasswordResponse)
async def change_password(
    data: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = AuthService(db)
    return await service.change_password(current_user, data)


@router.post("/check-email", response_model=CheckEmailResponse)
@limiter.limit(settings.RATE_LIMIT_CHECK_EMAIL)
async def check_email(request: Request, data: CheckEmailRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    return await service.check_email(data.email)


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(data: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    return await service.forgot_password(data)


@router.post("/verify-reset-otp", response_model=VerifyResetOtpResponse)
async def verify_reset_otp(data: VerifyResetOtpRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    return await service.verify_reset_otp(data)


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(data: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    return await service.reset_password(data)


@router.post("/resend-otp", response_model=MessageResponse)
async def resend_otp(data: ResendOtpRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    return await service.resend_otp(data)


# ─── Google sign-in (flow described in app/modules/auth/google.py) ──────────


@router.get("/google/url", response_model=GoogleAuthUrlResponse)
async def google_auth_url(
    platform: Literal["web", "mobile"],
    code_challenge: Optional[str] = Query(
        None,
        pattern=r"^[A-Za-z0-9_-]{43}$",
        description="base64url(sha256(code_verifier)); send code_verifier to /google/exchange",
    ),
):
    google.require_configured()
    return GoogleAuthUrlResponse(url=google.build_auth_url(platform, code_challenge))


@router.get("/google/authorized", include_in_schema=False)
async def google_authorized(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Google redirects the browser here; we redirect it on to the app."""
    google.require_configured()
    payload = google.decode_state(state)
    if payload is None:
        # Without a valid state we don't know which app to send the user back to.
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_state", "message": "Sign-in link is invalid or expired. Please try again."},
        )
    platform = payload["platform"]

    if error or not code:
        # e.g. error=access_denied when the user cancels on Google's screen.
        return RedirectResponse(google.client_redirect_url(platform, error=error or "missing_code"), status_code=302)

    try:
        exchange_code = await GoogleAuthService(db).complete_sign_in(code, payload)
    except GoogleAuthError as e:
        logger.warning(f"Google sign-in failed: {e.code}: {e}")
        return RedirectResponse(google.client_redirect_url(platform, error=e.code), status_code=302)

    return RedirectResponse(google.client_redirect_url(platform, code=exchange_code), status_code=302)


@router.post("/google/exchange", response_model=AuthResponse)
async def google_exchange(data: GoogleExchangeRequest, db: AsyncSession = Depends(get_db)):
    return await GoogleAuthService(db).exchange(data.code, data.code_verifier)
