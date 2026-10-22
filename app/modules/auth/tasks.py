import asyncio
import uuid as uuid_pkg

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.emails import deliver, render
from app.modules.auth.sms import deliver_sms, render_sms
from app.workers.registry import PermanentError, task


# inline_fallback: an OTP that never arrives leaves the user stuck, so if
# RabbitMQ is down the API sends it itself rather than failing the request.
@task("email.send_otp", queue="emails", max_retries=5, inline_fallback=True)
async def send_otp_email(kind: str, to: str, otp: str) -> None:
    try:
        subject, html = render(kind, otp)
    except ValueError as e:
        raise PermanentError(str(e))
    # Resend's client is synchronous; keep it off the event loop.
    await asyncio.to_thread(deliver, to, subject, html, f"{kind} OTP: {otp}")


@task("sms.send_otp", queue="sms", max_retries=5, inline_fallback=True)
async def send_otp_sms(kind: str, to: str, otp: str) -> None:
    try:
        message = render_sms(kind, otp)
    except ValueError as e:
        raise PermanentError(str(e))
    # The SMS gateway call is a blocking HTTP request; keep it off the event loop.
    await asyncio.to_thread(deliver_sms, to, message, f"{kind} OTP: {otp}")


@task("auth.import_google_avatar", queue="default", max_retries=2)
async def import_google_avatar(user_id: str, db: AsyncSession) -> None:
    """A new Google sign-up starts with their Google photo, fetched off the sign-in path.
    Nothing happens if they've set a picture since, or Google's photo can't be fetched."""
    from app.modules.auth.avatar import AvatarService
    from app.modules.auth.models import User

    user = await db.get(User, uuid_pkg.UUID(user_id))
    if user is None or user.avatar_key or not user.google_picture_url:
        return
    try:
        await AvatarService(db).from_google(user)
    except HTTPException:
        pass  # they can still pick it, or another photo, from their profile
