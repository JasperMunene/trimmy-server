"""OTP email templates and delivery. Sending happens in the worker: see app/modules/auth/tasks.py."""
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

VERIFICATION = "verification"
PASSWORD_RESET = "password_reset"


def render(kind: str, otp: str) -> tuple[str, str]:
    code = (
        '<p style="font-size: 32px; font-weight: bold; letter-spacing: 8px; text-align: center; '
        f'padding: 20px; background: #f5f5f5; border-radius: 8px;">{otp}</p>'
    )
    if kind == VERIFICATION:
        return "Verify your Trimmy account", f"""
            <h2>Welcome to Trimmy!</h2>
            <p>Your verification code is:</p>
            {code}
            <p>This code expires in 24 hours.</p>
            <p>If you didn't create an account, you can ignore this email.</p>
            """
    if kind == PASSWORD_RESET:
        return "Reset your Trimmy password", f"""
            <h2>Password Reset</h2>
            <p>Your password reset code is:</p>
            {code}
            <p>This code expires in 1 hour.</p>
            <p>If you didn't request a password reset, you can ignore this email.</p>
            """
    raise ValueError(f"Unknown email kind: {kind}")


def deliver(to: str, subject: str, html: str, log_hint: str = "") -> None:
    if not settings.RESEND_API_KEY:
        # Local dev without Resend: print the code so it can be copied from the logs.
        logger.info(f"[email not sent: no RESEND_API_KEY] to={to} subject={subject!r} {log_hint}")
        return

    import resend

    resend.api_key = settings.RESEND_API_KEY
    resend.Emails.send(
        {
            "from": settings.RESEND_FROM_EMAIL,
            "to": to,
            "subject": subject,
            "html": html,
        }
    )
