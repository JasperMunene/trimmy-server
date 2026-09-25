import hashlib
import logging
import secrets
import string
import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    verify_password,
)
from app.modules.auth.models import AuthToken, AuthType, RefreshToken, User
from app.modules.auth.schemas import (
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResendOtpRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserResponse,
)

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def register(self, data: RegisterRequest) -> AuthResponse:
        existing = await self.db.execute(
            select(User).where(User.email == data.email)
        )
        existing_user = existing.scalar_one_or_none()

        if existing_user:
            if existing_user.verified:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "email_taken", "message": "Email already registered"},
                )
            await self._delete_old_tokens(existing_user.id)
            otp = self._generate_otp(existing_user.id)
            await self.db.commit()
            await self._send_verification_email(existing_user.email, otp)
            tokens = self._create_tokens(existing_user)
            return AuthResponse(
                user=UserResponse.model_validate(existing_user),
                tokens=tokens,
            )

        user = User(
            email=data.email,
            password_hash=get_password_hash(data.password),
            first_name=data.first_name,
            last_name=data.last_name,
            phone=data.phone,
            auth_type=AuthType.EMAIL,
            verified=False,
        )
        self.db.add(user)
        await self.db.flush()

        otp = self._generate_otp(user.id)
        tokens = self._create_tokens(user)

        await self.db.commit()
        await self.db.refresh(user)

        await self._send_verification_email(user.email, otp)

        return AuthResponse(
            user=UserResponse.model_validate(user),
            tokens=tokens,
        )

    async def login(self, data: LoginRequest) -> AuthResponse:
        result = await self.db.execute(
            select(User).where(User.email == data.email)
        )
        user = result.scalar_one_or_none()

        if not user or not user.password_hash:
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_credentials", "message": "Invalid email or password"},
            )

        if not verify_password(data.password, user.password_hash):
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_credentials", "message": "Invalid email or password"},
            )

        if not user.verified:
            await self._delete_old_tokens(user.id)
            otp = self._generate_otp(user.id)
            await self.db.commit()
            await self._send_verification_email(user.email, otp)
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "not_verified",
                    "message": "Email not verified. A new verification code has been sent.",
                },
            )

        tokens = self._create_tokens(user)

        refresh_token_hash = self._hash_token(tokens.refresh_token)
        refresh_expires = datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
        db_refresh = RefreshToken(
            user_id=user.id,
            token_hash=refresh_token_hash,
            expires_at=refresh_expires,
        )
        self.db.add(db_refresh)
        await self.db.commit()

        return AuthResponse(
            user=UserResponse.model_validate(user),
            tokens=tokens,
        )

    async def refresh(self, refresh_token: str) -> TokenResponse:
        payload = decode_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_refresh_token", "message": "Invalid refresh token"},
            )

        user_id = payload.get("sub")
        token_hash = self._hash_token(refresh_token)

        result = await self.db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.user_id == user_id,
            )
        )
        db_token = result.scalar_one_or_none()

        if not db_token:
            raise HTTPException(
                status_code=401,
                detail={"error": "refresh_token_not_found", "message": "Refresh token not found"},
            )

        if db_token.revoked:
            await self._revoke_all_user_tokens(user_id)
            await self.db.commit()
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "refresh_token_reused",
                    "message": "Refresh token reuse detected. All sessions revoked.",
                },
            )

        now_utc = datetime.now(timezone.utc)
        expires_at = db_token.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now_utc:
            raise HTTPException(
                status_code=401,
                detail={"error": "refresh_token_expired", "message": "Refresh token expired"},
            )

        db_token.revoked = True

        user_result = await self.db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=401,
                detail={"error": "user_not_found", "message": "User not found"},
            )

        tokens = self._create_tokens(user)

        new_refresh_hash = self._hash_token(tokens.refresh_token)
        new_expires = datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
        new_db_token = RefreshToken(
            user_id=user.id,
            token_hash=new_refresh_hash,
            expires_at=new_expires,
        )
        self.db.add(new_db_token)
        await self.db.commit()

        return tokens

    async def verify_email(self, otp: str) -> AuthResponse:
        otp_hash = self._hash_token(otp)
        result = await self.db.execute(
            select(AuthToken).where(
                AuthToken.token_hash == otp_hash,
                AuthToken.purpose == "verification",
            )
        )
        auth_token = result.scalar_one_or_none()

        if not auth_token:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_otp", "message": "Invalid or expired verification code"},
            )

        age = datetime.now(timezone.utc) - auth_token.created_at.replace(tzinfo=timezone.utc)
        if age > timedelta(hours=24):
            await self.db.delete(auth_token)
            await self.db.commit()
            raise HTTPException(
                status_code=400,
                detail={"error": "otp_expired", "message": "Verification code expired"},
            )

        user_result = await self.db.execute(
            select(User).where(User.id == auth_token.user_id)
        )
        user = user_result.scalar_one_or_none()
        user.verified = True
        user.verified_at = datetime.now(timezone.utc)

        await self.db.delete(auth_token)
        tokens = self._create_tokens(user)

        refresh_hash = self._hash_token(tokens.refresh_token)
        refresh_expires = datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
        self.db.add(
            RefreshToken(user_id=user.id, token_hash=refresh_hash, expires_at=refresh_expires)
        )

        await self.db.commit()
        await self.db.refresh(user)

        return AuthResponse(
            user=UserResponse.model_validate(user),
            tokens=tokens,
        )

    async def forgot_password(self, data: ForgotPasswordRequest) -> dict:
        result = await self.db.execute(
            select(User).where(User.email == data.email)
        )
        user = result.scalar_one_or_none()

        if not user:
            return {"message": "If an account exists, a reset code has been sent."}

        await self._delete_tokens_by_purpose(user.id, "password_reset")
        otp = self._generate_otp(user.id, purpose="password_reset")
        await self.db.commit()
        await self._send_password_reset_email(user.email, otp)

        return {"message": "If an account exists, a reset code has been sent."}

    async def reset_password(self, data: ResetPasswordRequest) -> dict:
        otp_hash = self._hash_token(data.otp)
        result = await self.db.execute(
            select(AuthToken).where(
                AuthToken.token_hash == otp_hash,
                AuthToken.purpose == "password_reset",
            )
        )
        auth_token = result.scalar_one_or_none()

        if not auth_token:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_otp", "message": "Invalid or expired reset code"},
            )

        age = datetime.now(timezone.utc) - auth_token.created_at.replace(tzinfo=timezone.utc)
        if age > timedelta(hours=1):
            await self.db.delete(auth_token)
            await self.db.commit()
            raise HTTPException(
                status_code=400,
                detail={"error": "otp_expired", "message": "Reset code expired"},
            )

        user_result = await self.db.execute(
            select(User).where(User.id == auth_token.user_id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=400,
                detail={"error": "user_not_found", "message": "User not found"},
            )

        user.password_hash = get_password_hash(data.new_password)
        await self.db.delete(auth_token)
        await self._revoke_all_user_tokens(user.id)
        await self.db.commit()

        return {"message": "Password updated successfully"}

    async def resend_otp(self, data: ResendOtpRequest) -> dict:
        result = await self.db.execute(
            select(User).where(User.email == data.email)
        )
        user = result.scalar_one_or_none()

        if not user:
            return {"message": "If an account exists, a new code has been sent."}

        await self._delete_tokens_by_purpose(user.id, data.purpose)
        otp = self._generate_otp(user.id, purpose=data.purpose)
        await self.db.commit()

        if data.purpose == "password_reset":
            await self._send_password_reset_email(user.email, otp)
        else:
            await self._send_verification_email(user.email, otp)

        return {"message": "If an account exists, a new code has been sent."}

    async def _delete_tokens_by_purpose(self, user_id: uuid_pkg.UUID, purpose: str) -> None:
        result = await self.db.execute(
            select(AuthToken).where(
                AuthToken.user_id == user_id,
                AuthToken.purpose == purpose,
            )
        )
        for token in result.scalars():
            await self.db.delete(token)

    async def _delete_old_tokens(self, user_id: uuid_pkg.UUID) -> None:
        result = await self.db.execute(
            select(AuthToken).where(
                AuthToken.user_id == user_id,
                AuthToken.purpose == "verification",
            )
        )
        for token in result.scalars():
            await self.db.delete(token)

    async def _revoke_all_user_tokens(self, user_id: str) -> None:
        result = await self.db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked == False,
            )
        )
        for token in result.scalars():
            token.revoked = True

    def _generate_otp(self, user_id: uuid_pkg.UUID, purpose: str = "verification") -> str:
        otp = "".join(secrets.choice(string.digits) for _ in range(6))
        auth_token = AuthToken(
            user_id=user_id,
            token_hash=self._hash_token(otp),
            purpose=purpose,
        )
        self.db.add(auth_token)
        return otp

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _create_tokens(self, user: User) -> TokenResponse:
        access_token = create_access_token(
            data={"sub": str(user.id), "email": user.email}
        )
        refresh_token = create_refresh_token(data={"sub": str(user.id)})
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        )

    async def _send_verification_email(self, email: str, otp: str) -> None:
        if not settings.RESEND_API_KEY:
            logger.warning(
                "RESEND_API_KEY not configured — verification email NOT sent. "
                "OTP for %s: %s",
                email,
                otp,
            )
            return

        try:
            import resend

            resend.api_key = settings.RESEND_API_KEY
            result = resend.Emails.send(
                {
                    "from": settings.RESEND_FROM_EMAIL,
                    "to": email,
                    "subject": "Verify your Trimmy account",
                    "html": f"""
                    <h2>Welcome to Trimmy!</h2>
                    <p>Your verification code is:</p>
                    <p style="font-size: 32px; font-weight: bold; letter-spacing: 8px; text-align: center; padding: 20px; background: #f5f5f5; border-radius: 8px;">{otp}</p>
                    <p>This code expires in 24 hours.</p>
                    <p>If you didn't create an account, you can ignore this email.</p>
                    """,
                }
            )
            logger.info("Verification email sent to %s (resend id=%s)", email, result.get("id"))
        except Exception as e:
            logger.exception("Failed to send verification email to %s: %s", email, e)

    async def _send_password_reset_email(self, email: str, otp: str) -> None:
        if not settings.RESEND_API_KEY:
            logger.warning(
                "RESEND_API_KEY not configured — password reset email NOT sent. "
                "OTP for %s: %s",
                email,
                otp,
            )
            return

        try:
            import resend

            resend.api_key = settings.RESEND_API_KEY
            result = resend.Emails.send(
                {
                    "from": settings.RESEND_FROM_EMAIL,
                    "to": email,
                    "subject": "Reset your Trimmy password",
                    "html": f"""
                    <h2>Password Reset</h2>
                    <p>Your password reset code is:</p>
                    <p style="font-size: 32px; font-weight: bold; letter-spacing: 8px; text-align: center; padding: 20px; background: #f5f5f5; border-radius: 8px;">{otp}</p>
                    <p>This code expires in 1 hour.</p>
                    <p>If you didn't request a password reset, you can ignore this email.</p>
                    """,
                }
            )
            logger.info("Password reset email sent to %s (resend id=%s)", email, result.get("id"))
        except Exception as e:
            logger.exception("Failed to send password reset email to %s: %s", email, e)
