import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.modules.auth import google
from app.modules.auth.models import AuthToken, RefreshToken, User

CLIENT_ID = "test-client.apps.googleusercontent.com"


@pytest.fixture(autouse=True)
def google_configured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_AUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(settings, "GOOGLE_AUTH_CLIENT_SECRET", "secret")


@pytest.fixture
def google_claims(monkeypatch):
    """Stand in for Google's token endpoint. Tests tweak `claims` before the callback."""
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-sub-123",
        "email": "jane@gmail.com",
        "email_verified": True,
        "given_name": "Jane",
        "family_name": "Doe",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    seen_codes = []

    async def fake_fetch(code):
        seen_codes.append(code)
        return dict(claims)

    monkeypatch.setattr(google, "fetch_google_claims", fake_fetch)
    claims["_seen_codes"] = seen_codes
    return claims


def _query(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


async def _start(client, platform="mobile", code_challenge=None):
    params = {"platform": platform}
    if code_challenge:
        params["code_challenge"] = code_challenge
    response = await client.get("/api/v1/auth/google/url", params=params)
    assert response.status_code == 200
    return _query(response.json()["url"])


async def _authorize(client, google_claims, platform="mobile", code_challenge=None):
    """Run steps 1-3; returns the redirect the app receives."""
    params = await _start(client, platform, code_challenge)
    google_claims["nonce"] = params["nonce"]
    response = await client.get(
        "/api/v1/auth/google/authorized", params={"code": "google-code", "state": params["state"]},
    )
    assert response.status_code == 302
    return response.headers["location"]


async def _sign_in(client, google_claims, **kwargs):
    location = await _authorize(client, google_claims, **kwargs)
    code = _query(location)["code"]
    response = await client.post("/api/v1/auth/google/exchange", json={"code": code})
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_auth_url(client):
    response = await client.get("/api/v1/auth/google/url", params={"platform": "web"})
    url = response.json()["url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    q = _query(url)
    assert q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == settings.GOOGLE_REDIRECT_URI
    assert q["redirect_uri"].endswith("/api/v1/auth/google/authorized")
    assert q["scope"] == "openid email profile"
    assert google.decode_state(q["state"])["platform"] == "web"


@pytest.mark.asyncio
async def test_auth_url_requires_valid_platform(client):
    assert (await client.get("/api/v1/auth/google/url")).status_code == 422
    assert (await client.get("/api/v1/auth/google/url", params={"platform": "tv"})).status_code == 422


@pytest.mark.asyncio
async def test_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_AUTH_CLIENT_ID", "")
    response = await client.get("/api/v1/auth/google/url", params={"platform": "web"})
    assert response.status_code == 503
    assert response.json()["error"] == "google_not_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,base", [
    ("mobile", "GOOGLE_MOBILE_REDIRECT_URL"),
    ("web", "GOOGLE_WEB_REDIRECT_URL"),
])
async def test_redirects_to_platform(client, google_claims, platform, base):
    location = await _authorize(client, google_claims, platform=platform)
    assert location.startswith(getattr(settings, base) + "?code=")
    assert "access_token" not in location


@pytest.mark.asyncio
async def test_new_user_created(client, google_claims, db_session):
    data = await _sign_in(client, google_claims)
    assert data["user"]["email"] == "jane@gmail.com"
    assert data["user"]["first_name"] == "Jane"
    assert data["user"]["auth_type"] == "google"
    assert data["user"]["verified"] is True
    assert google_claims["_seen_codes"] == ["google-code"]

    # Tokens work like any other login.
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {data['tokens']['access_token']}"})
    assert me.json()["email"] == "jane@gmail.com"
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": data["tokens"]["refresh_token"]})).status_code == 200


@pytest.mark.asyncio
async def test_returning_user_matched_by_sub(client, google_claims, db_session):
    first = await _sign_in(client, google_claims)
    google_claims["email"] = "jane.new@gmail.com"  # changed on Google's side
    second = await _sign_in(client, google_claims)
    assert second["user"]["id"] == first["user"]["id"]


@pytest.mark.asyncio
async def test_links_existing_verified_account(client, google_claims, test_user, db_session):
    google_claims["email"] = test_user.email
    data = await _sign_in(client, google_claims)
    assert data["user"]["id"] == str(test_user.id)
    assert data["user"]["auth_type"] == "email"
    # Password login still works after linking.
    login = await client.post("/api/v1/auth/login", json={"email": test_user.email, "password": "testpass123"})
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_takes_over_unverified_account_safely(client, google_claims, db_session):
    # A squatter registered this email with a password and holds a session.
    squatter = await client.post("/api/v1/auth/register", json={
        "email": "squat@example.com", "password": "password123", "first_name": "S", "last_name": "Q",
    })
    squatter_refresh = squatter.json()["tokens"]["refresh_token"]
    google_claims["email"] = "squat@example.com"

    data = await _sign_in(client, google_claims)
    assert data["user"]["verified"] is True
    # The squatter's password and session no longer work.
    login = await client.post("/api/v1/auth/login", json={"email": "squat@example.com", "password": "password123"})
    assert login.status_code == 401
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": squatter_refresh})).status_code == 401


@pytest.mark.asyncio
async def test_exchange_code_is_single_use(client, google_claims):
    code = _query(await _authorize(client, google_claims))["code"]
    assert (await client.post("/api/v1/auth/google/exchange", json={"code": code})).status_code == 200
    response = await client.post("/api/v1/auth/google/exchange", json={"code": code})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_code"


@pytest.mark.asyncio
async def test_exchange_code_expires(client, google_claims, db_session):
    code = _query(await _authorize(client, google_claims))["code"]
    token = (await db_session.execute(select(AuthToken).where(AuthToken.purpose == "google_exchange"))).scalar_one()
    token.created_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    await db_session.commit()
    assert (await client.post("/api/v1/auth/google/exchange", json={"code": code})).status_code == 400


@pytest.mark.asyncio
async def test_code_challenge_enforced(client, google_claims):
    verifier = secrets.token_urlsafe(48)
    challenge = google.code_challenge_for(verifier)
    code = _query(await _authorize(client, google_claims, code_challenge=challenge))["code"]

    assert (await client.post("/api/v1/auth/google/exchange", json={"code": code})).status_code == 400
    wrong = secrets.token_urlsafe(48)
    assert (await client.post("/api/v1/auth/google/exchange", json={"code": code, "code_verifier": wrong})).status_code == 400
    ok = await client.post("/api/v1/auth/google/exchange", json={"code": code, "code_verifier": verifier})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_invalid_state_is_400(client, google_claims):
    response = await client.get("/api/v1/auth/google/authorized", params={"code": "c", "state": "forged"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_state"


@pytest.mark.asyncio
async def test_user_cancelled_redirects_with_error(client):
    state = (await _start(client, "web"))["state"]
    response = await client.get("/api/v1/auth/google/authorized", params={"error": "access_denied", "state": state})
    assert response.status_code == 302
    assert _query(response.headers["location"]) == {"error": "access_denied"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change,error", [
    ({"email_verified": False}, "email_not_verified"),
    ({"aud": "someone-else"}, "invalid_id_token"),
    ({"iss": "https://evil.example.com"}, "invalid_id_token"),
    ({"nonce": "replayed"}, "invalid_id_token"),
])
async def test_bad_id_token_redirects_with_error(client, google_claims, db_session, change, error):
    params = await _start(client)
    google_claims["nonce"] = params["nonce"]
    google_claims.update(change)
    response = await client.get(
        "/api/v1/auth/google/authorized", params={"code": "google-code", "state": params["state"]},
    )
    assert response.status_code == 302
    assert _query(response.headers["location"]) == {"error": error}
    assert (await db_session.execute(select(User))).first() is None
