import hashlib
import hmac
import logging
import math
import secrets
import string
import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    verify_password,
)
from app.modules.auth.models import AuthToken, AuthType, OtpSendLog, RefreshToken, User
from app.modules.auth.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    CheckEmailResponse,
    CompletePhoneRegistrationRequest,
    CompleteRegistrationRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResendOtpRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateMeRequest,
    UserResponse,
    VerifyEmailRequest,
    VerifyResetOtpRequest,
)
from app.modules.auth.tasks import send_otp_email, send_otp_sms
from app.core.phone_utils import normalise_phone, InvalidPhoneNumberError

logger = logging.getLogger(__name__)

OTP_TTL = {
    "verification": timedelta(hours=24),
    "password_reset": timedelta(hours=1),
}


def _aware(dt: datetime) -> datetime:
    # SQLite hands back naive datetimes; Postgres returns aware ones.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _invalid_credentials(status_code: int, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": "invalid_credentials", "message": message},
    )


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def register(self, data: RegisterRequest) -> AuthResponse:
        existing = await self._get_user_by_email(data.email)
        if existing:
            # Same answer whether or not the existing account is verified: never
            # hand out tokens for an account the caller hasn't proven they own.
            raise HTTPException(
                status_code=409,
                detail={"error": "email_taken", "message": "An account with this email already exists"},
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
        self._log_send(data.email, "verification")
        tokens = self._issue_tokens(user)

        await self.db.commit()
        await self.db.refresh(user)

        await send_otp_email.enqueue(kind="verification", to=user.email, otp=otp)

        return AuthResponse(user=UserResponse.model_validate(user), tokens=tokens)

    async def request_email_otp(self, email: str) -> dict:
        """Step 1 of email-first signup: send a verification OTP for an email.

        Creates a pending (password-less, unverified) account if none exists so
        the OTP has somewhere to attach. If the email already belongs to a
        verified account, refuse — that address is taken.
        """
        existing = await self._get_user_by_email(email)
        if existing and existing.verified:
            raise HTTPException(
                status_code=409,
                detail={"error": "email_taken", "message": "An account with this email already exists"},
            )

        # Rate-limit sends per email (same limiter as resend/forgot).
        retry_after = await self._send_retry_after(email, "verification")
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "too_many_requests",
                    "message": "Please wait before requesting another code.",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        user = existing
        if not user:
            # Pending account: no password yet, filled in at complete_registration.
            user = User(email=email, auth_type=AuthType.EMAIL, verified=False)
            self.db.add(user)
            await self.db.flush()

        await self._delete_tokens_by_purpose(user.id, "verification")
        otp = self._generate_otp(user.id, purpose="verification")
        self._log_send(email, "verification")
        await self.db.commit()

        await send_otp_email.enqueue(kind="verification", to=email, otp=otp)
        return {"message": "If the email is available, a verification code has been sent."}

    async def complete_registration(self, data: CompleteRegistrationRequest) -> AuthResponse:
        """Step 2 of email-first signup: verify the OTP and set the account's
        password + profile, then issue tokens.
        """
        user, auth_token = await self._check_otp(data.email, data.otp, "verification")

        if user.verified and user.password_hash:
            # Already a full, verified account — don't let this path hijack it.
            raise HTTPException(
                status_code=409,
                detail={"error": "email_taken", "message": "An account with this email already exists"},
            )

        if data.phone:
            taken = await self.db.execute(
                select(User.id).where(User.phone == data.phone, User.id != user.id)
            )
            if taken.first():
                raise HTTPException(
                    status_code=409,
                    detail={"error": "phone_taken", "message": "Phone number is already in use"},
                )

        user.password_hash = get_password_hash(data.password)
        user.first_name = data.first_name
        user.last_name = data.last_name
        user.phone = data.phone
        user.verified = True
        user.verified_at = datetime.now(timezone.utc)

        await self.db.delete(auth_token)
        tokens = self._issue_tokens(user)

        await self.db.commit()
        await self.db.refresh(user)

        return AuthResponse(user=UserResponse.model_validate(user), tokens=tokens)


    async def request_phone_otp(self, raw_phone: str) -> dict:
        """Step 1 of phone-first signup: send a verification OTP via SMS.

        Mirrors request_email_otp: creates a pending (password-less) account so
        the OTP has somewhere to attach. Refuses if the number already belongs
        to a verified account.
        """
        try:
            phone = normalise_phone(raw_phone, default_country=settings.SMS_DEFAULT_COUNTRY)
        except InvalidPhoneNumberError:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_phone", "message": "Enter a valid phone number"},
            )

        existing = await self._get_user_by_phone(phone)
        if existing and existing.verified:
            raise HTTPException(
                status_code=409,
                detail={"error": "phone_taken", "message": "An account with this phone number already exists"},
            )

        # Rate-limit sends per phone (OtpSendLog keys on a string identifier).
        retry_after = await self._send_retry_after(phone, "verification")
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "too_many_requests",
                    "message": "Please wait before requesting another code.",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        user = existing
        if not user:
            user = User(phone=phone, auth_type=AuthType.PHONE, verified=False)
            self.db.add(user)
            await self.db.flush()

        await self._delete_tokens_by_purpose(user.id, "verification")
        otp = self._generate_otp(user.id, purpose="verification")
        self._log_send(phone, "verification")
        await self.db.commit()

        await send_otp_sms.enqueue(kind="verification", to=phone, otp=otp)
        return {"message": "If the number is available, a verification code has been sent."}

    async def complete_phone_registration(
        self, data: CompletePhoneRegistrationRequest
    ) -> AuthResponse:
        """Step 2 of phone-first signup: verify the SMS OTP, set password +
        profile, then issue tokens.
        """
        try:
            phone = normalise_phone(data.phone, default_country=settings.SMS_DEFAULT_COUNTRY)
        except InvalidPhoneNumberError:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_phone", "message": "Enter a valid phone number"},
            )

        user = await self._get_user_by_phone(phone)
        user, auth_token = await self._check_otp_user(user, data.otp, "verification")

        if user.verified and user.password_hash:
            raise HTTPException(
                status_code=409,
                detail={"error": "phone_taken", "message": "An account with this phone number already exists"},
            )

        if data.email:
            taken = await self.db.execute(
                select(User.id).where(User.email == data.email, User.id != user.id)
            )
            if taken.first():
                raise HTTPException(
                    status_code=409,
                    detail={"error": "email_taken", "message": "Email is already in use"},
                )

        user.password_hash = get_password_hash(data.password)
        user.first_name = data.first_name
        user.last_name = data.last_name
        user.email = data.email
        user.verified = True
        user.verified_at = datetime.now(timezone.utc)

        await self.db.delete(auth_token)
        tokens = self._issue_tokens(user)

        await self.db.commit()
        await self.db.refresh(user)

        return AuthResponse(user=UserResponse.model_validate(user), tokens=tokens)


    async def login(self, data: LoginRequest) -> AuthResponse:
        user = await self._get_user_by_email(data.email)

        if not user or not user.password_hash or not verify_password(data.password, user.password_hash):
            raise _invalid_credentials(401, "Invalid email or password")

        if not user.verified:
            # Only replace the code if we're allowed to send a new one; otherwise
            # the user would be left holding a code we just invalidated.
            if await self._send_allowed(user.email, "verification"):
                await self._delete_tokens_by_purpose(user.id, "verification")
                otp = self._generate_otp(user.id)
                self._log_send(user.email, "verification")
                await self.db.commit()
                await send_otp_email.enqueue(kind="verification", to=user.email, otp=otp)
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "not_verified",
                    "message": "Email not verified. A new verification code has been sent.",
                },
            )

        tokens = self._issue_tokens(user)
        await self.db.commit()

        return AuthResponse(user=UserResponse.model_validate(user), tokens=tokens)

    async def refresh(self, refresh_token: str) -> TokenResponse:
        payload = decode_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_refresh_token", "message": "Invalid refresh token"},
            )

        user_id = payload.get("sub")
        db_token = await self._get_refresh_token(refresh_token)

        if not db_token or str(db_token.user_id) != user_id:
            raise HTTPException(
                status_code=401,
                detail={"error": "refresh_token_not_found", "message": "Refresh token not found"},
            )

        if db_token.revoked:
            await self._revoke_all_user_tokens(db_token.user_id)
            await self.db.commit()
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "refresh_token_reused",
                    "message": "Refresh token reuse detected. All sessions revoked.",
                },
            )

        if _aware(db_token.expires_at) < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=401,
                detail={"error": "refresh_token_expired", "message": "Refresh token expired"},
            )

        db_token.revoked = True

        user = await self.db.get(User, db_token.user_id)
        if not user:
            raise HTTPException(
                status_code=401,
                detail={"error": "user_not_found", "message": "User not found"},
            )

        tokens = self._issue_tokens(user)
        await self.db.commit()
        return tokens

    async def logout(self, user: User, refresh_token: str) -> None:
        # Idempotent: an unknown or someone else's token is a no-op. The row is
        # deleted rather than marked revoked so a later replay reads as "not found"
        # instead of tripping rotation reuse detection (which would end every session).
        db_token = await self._get_refresh_token(refresh_token)
        if db_token and db_token.user_id == user.id:
            await self.db.delete(db_token)
            await self.db.commit()

    async def verify_email(self, data: VerifyEmailRequest) -> AuthResponse:
        user, auth_token = await self._check_otp(data.email, data.otp, "verification")

        user.verified = True
        user.verified_at = datetime.now(timezone.utc)
        await self.db.delete(auth_token)
        tokens = self._issue_tokens(user)

        await self.db.commit()
        await self.db.refresh(user)

        return AuthResponse(user=UserResponse.model_validate(user), tokens=tokens)

    async def forgot_password(self, data: ForgotPasswordRequest) -> dict:
        await self._send_otp(data.email, "password_reset")
        return {"message": "If an account exists, a reset code has been sent."}

    async def verify_reset_otp(self, data: VerifyResetOtpRequest) -> dict:
        # Checks without consuming, but a wrong guess still counts as an attempt.
        await self._check_otp(data.email, data.otp, "password_reset")
        return {"valid": True}

    async def reset_password(self, data: ResetPasswordRequest) -> dict:
        user, auth_token = await self._check_otp(data.email, data.otp, "password_reset")

        user.password_hash = get_password_hash(data.new_password)
        await self.db.delete(auth_token)
        await self._end_all_sessions(user.id)
        await self.db.commit()

        return {"message": "Password updated successfully"}

    async def resend_otp(self, data: ResendOtpRequest) -> dict:
        await self._send_otp(data.email, data.purpose)
        return {"message": "If an account exists, a new code has been sent."}

    async def check_email(self, email: str) -> CheckEmailResponse:
        return CheckEmailResponse(exists=await self._get_user_by_email(email) is not None)

    async def update_me(self, user: User, data: UpdateMeRequest) -> User:
        fields = data.model_fields_set
        for name in ("first_name", "last_name"):
            if name in fields:
                if getattr(data, name) is None:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "invalid_field", "message": f"{name} cannot be null"},
                    )
                setattr(user, name, getattr(data, name))

        if "phone" in fields and data.phone != user.phone:
            if data.phone is not None:
                taken = await self.db.execute(
                    select(User.id).where(User.phone == data.phone, User.id != user.id)
                )
                if taken.first():
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "phone_taken", "message": "Phone number is already in use"},
                    )
            user.phone = data.phone

        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def change_password(self, user: User, data: ChangePasswordRequest) -> ChangePasswordResponse:
        if not user.password_hash or not verify_password(data.current_password, user.password_hash):
            raise _invalid_credentials(400, "Current password is incorrect")

        user.password_hash = get_password_hash(data.new_password)
        await self._end_all_sessions(user.id)
        tokens = self._issue_tokens(user)
        await self.db.commit()

        return ChangePasswordResponse(message="Password updated", tokens=tokens)

    async def delete_account(self, user: User, password: str) -> None:
        if not user.password_hash or not verify_password(password, user.password_hash):
            raise _invalid_credentials(400, "Password is incorrect")

        from app.modules.business.models import Business, BusinessPhoto, Staff
        from app.modules.business.tasks import delete_stored_object

        # Staff rows reference users without a FK, so clear them explicitly.
        await self.db.execute(delete(Staff).where(Staff.user_id == user.id))
        photo_keys = [
            key
            for key in (
                await self.db.execute(
                    select(BusinessPhoto.storage_key)
                    .join(Business, Business.id == BusinessPhoto.business_id)
                    .where(Business.owner_id == user.id, BusinessPhoto.storage_key.is_not(None))
                )
            ).scalars()
        ]
        # Delete owned businesses through the ORM so their child rows go too,
        # regardless of whether the DB enforces ON DELETE CASCADE (SQLite doesn't).
        owned = await self.db.execute(select(Business).where(Business.owner_id == user.id))
        for business in owned.scalars():
            await self.db.delete(business)
        await self.db.delete(user)
        await self.db.commit()

        for key in photo_keys:
            await delete_stored_object.enqueue(key=key)

    # ─── OTP helpers ──────────────────────────────────────────

    async def _send_otp(self, email: str, purpose: str) -> None:
        """Rate-limited (re)send used by forgot-password and resend-otp.

        Limits are keyed by the requested email, and a send is logged even when
        no account exists, so a 429 doesn't reveal whether the email is registered.
        """
        retry_after = await self._send_retry_after(email, purpose)
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "too_many_requests",
                    "message": "Please wait before requesting another code.",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        self._log_send(email, purpose)
        user = await self._get_user_by_email(email)
        if not user:
            await self.db.commit()
            return

        await self._delete_tokens_by_purpose(user.id, purpose)
        otp = self._generate_otp(user.id, purpose=purpose)
        await self.db.commit()

        await send_otp_email.enqueue(kind=purpose, to=user.email, otp=otp)

    async def _send_allowed(self, email: str, purpose: str) -> bool:
        return await self._send_retry_after(email, purpose) == 0

    async def _send_retry_after(self, email: str, purpose: str) -> int:
        """Seconds until another code may be sent to this email, or 0 if allowed now."""
        now = datetime.now(timezone.utc)
        hour_ago = now - timedelta(hours=1)

        await self.db.execute(
            delete(OtpSendLog).where(
                OtpSendLog.email == email,
                OtpSendLog.purpose == purpose,
                OtpSendLog.created_at < hour_ago,
            )
        )
        result = await self.db.execute(
            select(OtpSendLog.created_at)
            .where(
                OtpSendLog.email == email,
                OtpSendLog.purpose == purpose,
                OtpSendLog.created_at >= hour_ago,
            )
            .order_by(OtpSendLog.created_at.asc())
        )
        sent = [_aware(ts) for ts in result.scalars()]
        if not sent:
            return 0

        waits = []
        cooldown_ends = sent[-1] + timedelta(seconds=settings.OTP_RESEND_COOLDOWN_SECONDS)
        if cooldown_ends > now:
            waits.append(cooldown_ends - now)
        if len(sent) >= settings.OTP_MAX_SENDS_PER_HOUR:
            # The window frees up when the oldest send that still counts ages out.
            oldest = sent[len(sent) - settings.OTP_MAX_SENDS_PER_HOUR]
            waits.append(oldest + timedelta(hours=1) - now)
        if not waits:
            return 0
        return max(1, math.ceil(max(waits).total_seconds()))

    def _log_send(self, email: str, purpose: str) -> None:
        self.db.add(OtpSendLog(email=email, purpose=purpose))

    async def _check_otp(self, email: str, otp: str, purpose: str) -> tuple[User, AuthToken]:
        """Validate an OTP for the account owning `email`. See _check_otp_user."""
        user = await self._get_user_by_email(email)
        return await self._check_otp_user(user, otp, purpose)

    async def _check_otp_user(
        self, user: Optional[User], otp: str, purpose: str
    ) -> tuple[User, AuthToken]:
        """Validate an OTP for a specific account, counting failed attempts.

        After OTP_MAX_ATTEMPTS wrong guesses the code is deleted and the user has
        to request a new one, so a code can't be brute-forced.
        """
        noun = "verification" if purpose == "verification" else "reset"
        invalid = HTTPException(
            status_code=400,
            detail={"error": "invalid_otp", "message": f"Invalid or expired {noun} code"},
        )

        if not user:
            raise invalid

        result = await self.db.execute(
            select(AuthToken)
            .where(AuthToken.user_id == user.id, AuthToken.purpose == purpose)
            .order_by(AuthToken.created_at.desc())
            .limit(1)
        )
        auth_token = result.scalar_one_or_none()
        if not auth_token or auth_token.attempts >= settings.OTP_MAX_ATTEMPTS:
            raise invalid

        if datetime.now(timezone.utc) - _aware(auth_token.created_at) > OTP_TTL[purpose]:
            await self.db.delete(auth_token)
            await self.db.commit()
            raise HTTPException(
                status_code=400,
                detail={"error": "otp_expired", "message": f"{noun.capitalize()} code expired"},
            )

        if not hmac.compare_digest(auth_token.token_hash, self._hash_token(otp)):
            # Atomic increment so parallel guesses can't share one attempt.
            await self.db.execute(
                update(AuthToken)
                .where(AuthToken.id == auth_token.id)
                .values(attempts=AuthToken.attempts + 1)
            )
            await self.db.refresh(auth_token, ["attempts"])
            if auth_token.attempts >= settings.OTP_MAX_ATTEMPTS:
                await self.db.delete(auth_token)
                await self.db.commit()
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "otp_attempts_exceeded",
                        "message": "Too many incorrect attempts. Request a new code.",
                    },
                )
            await self.db.commit()
            raise invalid

        return user, auth_token

    async def _delete_tokens_by_purpose(self, user_id: uuid_pkg.UUID, purpose: str) -> None:
        await self.db.execute(
            delete(AuthToken).where(AuthToken.user_id == user_id, AuthToken.purpose == purpose)
        )

    def _generate_otp(self, user_id: uuid_pkg.UUID, purpose: str = "verification") -> str:
        otp = "".join(secrets.choice(string.digits) for _ in range(6))
        self.db.add(AuthToken(user_id=user_id, token_hash=self._hash_token(otp), purpose=purpose))
        return otp

    # ─── Token helpers ────────────────────────────────────────

    async def _get_user_by_email(self, email: str) -> Optional[User]:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def _get_user_by_phone(self, phone: str) -> Optional[User]:
        result = await self.db.execute(select(User).where(User.phone == phone))
        return result.scalar_one_or_none()

    async def _get_refresh_token(self, refresh_token: str) -> Optional[RefreshToken]:
        result = await self.db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == self._hash_token(refresh_token))
        )
        return result.scalar_one_or_none()

    async def _revoke_all_user_tokens(self, user_id: uuid_pkg.UUID) -> None:
        await self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked == False)  # noqa: E712
            .values(revoked=True)
        )

    async def _end_all_sessions(self, user_id: uuid_pkg.UUID) -> None:
        # Delete (not revoke) so other devices' stale tokens get a plain 401 on
        # refresh without triggering reuse detection against the new session.
        await self.db.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _issue_tokens(self, user: User) -> TokenResponse:
        """Mint an access/refresh pair and persist the refresh token (caller commits)."""
        access_token = create_access_token(data={"sub": str(user.id), "email": user.email})
        refresh_token = create_refresh_token(data={"sub": str(user.id)})
        self.db.add(
            RefreshToken(
                user_id=user.id,
                token_hash=self._hash_token(refresh_token),
                expires_at=datetime.now(timezone.utc)
                + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            )
        )
        return TokenResponse(access_token=access_token, refresh_token=refresh_token)
