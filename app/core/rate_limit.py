"""Request limits, counted in Redis so they hold across API processes and restarts.

Keyed by client IP. Behind nginx that's the real client only when uvicorn trusts the proxy's
X-Forwarded-For (FORWARDED_ALLOW_IPS, set in the compose files); otherwise every request looks
like it comes from the proxy and everyone shares one count.
"""
import logging

from limits import parse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings

logger = logging.getLogger(__name__)

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.RATE_LIMIT_STORAGE or settings.REDIS_URL,
    storage_options={"socket_connect_timeout": 1, "socket_timeout": 1},
    in_memory_fallback_enabled=True,  # Redis down: count per process rather than fail requests
    key_prefix="rl",
)

_LOGIN_FAILURES = parse(settings.LOGIN_MAX_FAILURES)


def _login_key(email: str) -> str:
    return email.strip().lower()


def login_locked(email: str) -> bool:
    """Too many wrong passwords for this email lately. Storage errors never lock anyone out."""
    try:
        return not limiter.limiter.test(_LOGIN_FAILURES, "login-failures", _login_key(email))
    except Exception as err:
        logger.warning("Login failure count unavailable: %s", err)
        return False


def record_login_failure(email: str) -> None:
    try:
        limiter.limiter.hit(_LOGIN_FAILURES, "login-failures", _login_key(email))
    except Exception as err:
        logger.warning("Couldn't count a login failure: %s", err)


def clear_login_failures(email: str) -> None:
    try:
        limiter.limiter.clear(_LOGIN_FAILURES, "login-failures", _login_key(email))
    except Exception:
        pass
