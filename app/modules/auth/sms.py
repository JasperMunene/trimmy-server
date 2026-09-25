"""OTP SMS templates and delivery. Sending happens in the worker: see app/modules/auth/tasks.py."""
import logging

import httpx

from app.core.config import settings
from app.core.phone_utils import normalise_for_sms

logger = logging.getLogger(__name__)

VERIFICATION = "verification"
PASSWORD_RESET = "password_reset"

_HOSTPINNACLE_URL = "https://smsportal.hostpinnacle.co.ke/SMSApi/send"


def render_sms(kind: str, otp: str) -> str:
    if kind == VERIFICATION:
        return f"Your Trimmy verification code is {otp}. It expires in 24 hours."
    if kind == PASSWORD_RESET:
        return f"Your Trimmy password reset code is {otp}. It expires in 1 hour."
    raise ValueError(f"Unknown sms kind: {kind}")


def deliver_sms(to: str, message: str, log_hint: str = "") -> None:
    if not settings.SMS_API_KEY:
        # Local dev without SMS creds: print the code so it can be copied from the logs.
        logger.info(f"[sms not sent: no SMS_API_KEY] to={to} {log_hint}")
        return

    mobile = normalise_for_sms(to, default_country=settings.SMS_DEFAULT_COUNTRY)
    payload = {
        "userid": settings.SMS_USER_ID,
        "password": settings.SMS_PASSWORD,
        "mobile": mobile,
        "msg": message,
        "senderid": settings.SMS_SENDER_ID,
        "msgType": "text",
        "duplicatecheck": "true",
        "output": "json",
        "sendMethod": "quick",
    }
    headers = {
        "apikey": settings.SMS_API_KEY,
        "cache-control": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
    }
    response = httpx.post(_HOSTPINNACLE_URL, data=payload, headers=headers, timeout=10)
    response.raise_for_status()
    logger.info(f"SMS sent to {mobile}")
