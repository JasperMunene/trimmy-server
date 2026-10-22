"""Google sign-in (OAuth 2.0 authorization-code flow, server side).

1. App:    GET  /auth/google/url?platform=web|mobile[&code_challenge=...][&redirect_uri=...]  -> { url }
           A web app can name where to come back to (redirect_uri); it must be on
           GOOGLE_WEB_REDIRECT_ALLOWLIST, and travels in the signed state.
2. App opens `url`; the user signs in with Google.
3. Google: GET  /auth/google/authorized?code=...&state=...
           Server exchanges the code with Google, finds/links/creates the user,
           then redirects to the app with a one-time exchange code:
             mobile -> GOOGLE_MOBILE_REDIRECT_URL?code=...   (trimmy://auth/google)
             web    -> the requested redirect_uri, else GOOGLE_WEB_REDIRECT_URL, ?code=...
           or, on failure, ?error=<code>.
4. App:    POST /auth/google/exchange { code[, code_verifier] } -> { user, tokens }

Tokens never travel in a URL. The exchange code is single-use and short-lived.
If the app sends a code_challenge (base64url(sha256(verifier)), as in PKCE) in
step 1, it must send the matching code_verifier in step 4, so another app that
registers the same URL scheme can't redeem an intercepted code.
"""
import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.auth.models import AuthToken, AuthType, RefreshToken, User
from app.modules.auth.schemas import AuthResponse, UserResponse

logger = logging.getLogger(__name__)

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

STATE_TTL = timedelta(minutes=10)
EXCHANGE_CODE_TTL = timedelta(minutes=2)
EXCHANGE_PURPOSE = "google_exchange"
PLATFORMS = ("web", "mobile")


class GoogleAuthError(Exception):
    """A sign-in failure to report back to the app as ?error=<code>."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def require_configured() -> None:
    if not (settings.GOOGLE_AUTH_CLIENT_ID and settings.GOOGLE_AUTH_CLIENT_SECRET and settings.GOOGLE_REDIRECT_URI):
        raise HTTPException(
            status_code=503,
            detail={"error": "google_not_configured", "message": "Google sign-in is not configured"},
        )


def allowed_web_redirect(redirect_uri: str) -> bool:
    """Exact matches only: a prefix or pattern could let a code reach another site."""
    allowed = set(settings.GOOGLE_WEB_REDIRECT_ALLOWLIST)
    if settings.GOOGLE_WEB_REDIRECT_URL:
        allowed.add(settings.GOOGLE_WEB_REDIRECT_URL)
    return redirect_uri in allowed


def build_auth_url(platform: str, code_challenge: Optional[str], redirect_uri: Optional[str] = None) -> str:
    if redirect_uri is not None and (platform != "web" or not allowed_web_redirect(redirect_uri)):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_redirect", "message": "This app isn't allowed to receive Google sign-ins"},
        )
    nonce = secrets.token_urlsafe(16)
    state = jwt.encode(
        {
            "type": "google_state",
            "platform": platform,
            "nonce": nonce,
            "cc": code_challenge,
            "ru": redirect_uri,
            "exp": datetime.now(timezone.utc) + STATE_TTL,
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    params = {
        "client_id": settings.GOOGLE_AUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
        "access_type": "online",
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


def decode_state(state: Optional[str]) -> Optional[dict]:
    if not state:
        return None
    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "google_state" or payload.get("platform") not in PLATFORMS:
        return None
    return payload


def client_redirect_url(platform: str, redirect_uri: Optional[str] = None, **params: str) -> str:
    if platform == "mobile":
        base = settings.GOOGLE_MOBILE_REDIRECT_URL
    elif redirect_uri and allowed_web_redirect(redirect_uri):  # re-checked: the allow-list may have changed
        base = redirect_uri
    else:
        base = settings.GOOGLE_WEB_REDIRECT_URL or f"{settings.CLIENT_URL.rstrip('/')}/auth/google"
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


def code_challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def fetch_google_claims(code: str) -> dict:
    """Swap the authorization code for Google's ID token and return its claims."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_AUTH_CLIENT_ID,
                    "client_secret": settings.GOOGLE_AUTH_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as e:
        raise GoogleAuthError("google_exchange_failed", f"token request failed: {e}")
    if response.status_code != 200:
        raise GoogleAuthError("google_exchange_failed", f"token endpoint {response.status_code}: {response.text[:300]}")

    id_token = response.json().get("id_token")
    if not id_token:
        raise GoogleAuthError("google_exchange_failed", "no id_token in token response")
    # The token came straight from Google's token endpoint over TLS, which OpenID
    # Connect accepts in place of verifying its signature (Core 1.0 §3.1.3.7).
    # The claims are still checked in validate_claims.
    return jwt.decode(id_token, options={"verify_signature": False})


def validate_claims(claims: dict, nonce: str) -> None:
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise GoogleAuthError("invalid_id_token", f"unexpected issuer {claims.get('iss')}")
    aud = claims.get("aud")
    if settings.GOOGLE_AUTH_CLIENT_ID not in (aud if isinstance(aud, list) else [aud]):
        raise GoogleAuthError("invalid_id_token", "audience mismatch")
    if int(claims.get("exp", 0)) < datetime.now(timezone.utc).timestamp():
        raise GoogleAuthError("invalid_id_token", "id_token expired")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise GoogleAuthError("invalid_id_token", "nonce mismatch")
    if not claims.get("sub") or not claims.get("email"):
        raise GoogleAuthError("invalid_id_token", "missing sub or email")
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("email_not_verified", "Google account email is not verified")


def _hash_exchange_code(code: str, code_challenge: Optional[str]) -> str:
    # Binding the challenge into the stored hash means a code only redeems with
    # the matching verifier (or with none, if no challenge was sent).
    return hashlib.sha256(f"{code}:{code_challenge or ''}".encode()).hexdigest()


class GoogleAuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def complete_sign_in(self, code: str, state: dict) -> str:
        """Handle Google's redirect; returns the one-time code to hand to the app."""
        claims = await fetch_google_claims(code)
        validate_claims(claims, state["nonce"])
        user = await self._find_or_link_user(claims)

        exchange_code = secrets.token_urlsafe(32)
        self.db.add(
            AuthToken(
                user_id=user.id,
                token_hash=_hash_exchange_code(exchange_code, state.get("cc")),
                purpose=EXCHANGE_PURPOSE,
            )
        )
        await self.db.commit()
        return exchange_code

    async def exchange(self, code: str, code_verifier: Optional[str]) -> AuthResponse:
        from app.modules.auth.service import AuthService, _aware

        challenge = code_challenge_for(code_verifier) if code_verifier else None
        result = await self.db.execute(
            select(AuthToken).where(
                AuthToken.purpose == EXCHANGE_PURPOSE,
                AuthToken.token_hash == _hash_exchange_code(code, challenge),
            )
        )
        auth_token = result.scalar_one_or_none()
        invalid = HTTPException(
            status_code=400,
            detail={"error": "invalid_code", "message": "Invalid or expired sign-in code"},
        )
        if not auth_token:
            raise invalid

        # Single use, whether or not it's still fresh.
        await self.db.delete(auth_token)
        if datetime.now(timezone.utc) - _aware(auth_token.created_at) > EXCHANGE_CODE_TTL:
            await self.db.commit()
            raise invalid

        user = await self.db.get(User, auth_token.user_id)
        if not user:
            await self.db.commit()
            raise invalid

        tokens = AuthService(self.db)._issue_tokens(user)
        await self.db.commit()
        await self.db.refresh(user)
        return AuthResponse(user=UserResponse.model_validate(user), tokens=tokens)

    async def _claim(self, user: User) -> None:
        """Google proved the email: bookings a business made for it become theirs (customer_match.py)."""
        from app.modules.booking.customer_match import claim_bookings

        await claim_bookings(self.db, user)

    async def _find_or_link_user(self, claims: dict) -> User:
        sub, email = str(claims["sub"]), claims["email"]
        now = datetime.now(timezone.utc)

        result = await self.db.execute(select(User).where(User.google_sub == sub))
        user = result.scalar_one_or_none()
        if user:
            return user

        result = await self.db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user:
            if user.google_sub and user.google_sub != sub:
                raise GoogleAuthError("account_conflict", "email is linked to a different Google account")
            if not user.verified:
                # Someone registered this email with a password but never proved
                # they own it; Google just did. Discard that password and its
                # sessions so a squatter who pre-registered the email can't get in.
                user.password_hash = None
                await self.db.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
                user.verified = True
                user.verified_at = now
            user.google_sub = sub
            await self._claim(user)
            return user

        user = User(
            email=email,
            first_name=claims.get("given_name"),
            last_name=claims.get("family_name"),
            auth_type=AuthType.GOOGLE,
            google_sub=sub,
            verified=True,
            verified_at=now,
        )
        self.db.add(user)
        await self.db.flush()
        await self._claim(user)
        return user
