"""Admin foundation: 2-step sign-in, require_admin, disabling users, team, audit log."""
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core import totp
from app.core.audit import AuditLog
from app.core.security import get_password_hash
from app.modules.auth.models import AuthType, RefreshToken, User

ADMIN_EMAIL = "ops@trimmyadmin.com"
ADMIN_PASSWORD = "admin-pass-123"


@pytest_asyncio.fixture
async def admin(db_session):
    user = User(id=uuid.uuid4(), email=ADMIN_EMAIL, password_hash=get_password_hash(ADMIN_PASSWORD),
                first_name="Ops", last_name="Admin", auth_type=AuthType.EMAIL, verified=True, platform_role="admin")
    db_session.add(user)
    await db_session.commit()
    return user


def now_code(secret: str, offset_steps: int = 0) -> str:
    return totp.code_at(secret, int(time.time() // totp.STEP_SECONDS) + offset_steps)


async def sign_in(client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD) -> dict:
    """Full first sign-in (enroll), returns {'session':..., 'secret':...}."""
    start = await client.post("/api/v1/admin/auth/login", json={"email": email, "password": password})
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["step"] == "enroll" and body["otpauthUrl"].startswith("otpauth://totp/")
    done = await client.post("/api/v1/admin/auth/verify", json={"challenge": body["challenge"], "code": now_code(body["secret"])})
    assert done.status_code == 200, done.text
    return {"session": done.json(), "secret": body["secret"]}


def bearer(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['tokens']['access_token']}"}


# ─── TOTP ────────────────────────────────────────────────────


def test_totp_matches_the_rfc_test_vector():
    # RFC 6238 appendix B (SHA-1, secret "12345678901234567890"), last 6 digits.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.code_at(secret, 1111111109 // 30) == "081804"


def test_a_code_works_once_and_secrets_round_trip_encrypted():
    secret = totp.new_secret()
    step = totp.verify(secret, now_code(secret))
    assert step is not None
    assert totp.verify(secret, now_code(secret), last_step=step) is None
    sealed = totp.encrypt(secret)
    assert secret not in sealed and totp.decrypt(sealed) == secret
    assert totp.decrypt("not-a-token") is None


# ─── Sign-in ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_sign_in_sets_up_the_authenticator_then_asks_for_codes(client, admin, db_session):
    first = await sign_in(client)
    assert first["session"]["user"]["email"] == ADMIN_EMAIL
    me = await client.get("/api/v1/admin/me", headers=bearer(first["session"]))
    assert me.status_code == 200 and me.json()["twoStepSince"]

    again = (await client.post("/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})).json()
    assert again["step"] == "totp" and again["secret"] is None
    # The code just used is spent; the next one works.
    reused = await client.post("/api/v1/admin/auth/verify", json={"challenge": again["challenge"], "code": now_code(first["secret"])})
    assert reused.status_code == 401 and reused.json()["error"] == "invalid_code"
    fresh = await client.post("/api/v1/admin/auth/verify", json={"challenge": again["challenge"], "code": now_code(first["secret"], 1)})
    assert fresh.status_code == 200

    actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("admin.sign_in") == 2 and "admin.two_step_setup" in actions


@pytest.mark.asyncio
async def test_non_admins_and_wrong_passwords_look_the_same(client, admin, test_user):
    not_admin = await client.post("/api/v1/admin/auth/login", json={"email": "test@example.com", "password": "testpass123"})
    wrong = await client.post("/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL, "password": "nope"})
    assert not_admin.status_code == wrong.status_code == 401
    assert not_admin.json() == wrong.json()


@pytest.mark.asyncio
async def test_app_tokens_and_half_finished_sign_ins_never_reach_admin(client, admin, auth_headers):
    assert (await client.get("/api/v1/admin/me", headers=auth_headers)).status_code == 401
    challenge = (await client.post("/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})).json()["challenge"]
    assert (await client.get("/api/v1/admin/me", headers={"Authorization": f"Bearer {challenge}"})).status_code == 401
    # An admin's own app sign-in (no second step) isn't enough either.
    app_login = (await client.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})).json()
    app_token = {"Authorization": f"Bearer {app_login['tokens']['access_token']}"}
    assert (await client.get("/api/v1/admin/me", headers=app_token)).status_code == 401


@pytest.mark.asyncio
async def test_admin_sessions_rotate_and_a_reused_refresh_ends_them(client, admin):
    session = (await sign_in(client))["session"]
    refresh = session["tokens"]["refresh_token"]
    rotated = await client.post("/api/v1/admin/auth/refresh", json={"refresh_token": refresh})
    assert rotated.status_code == 200
    reused = await client.post("/api/v1/admin/auth/refresh", json={"refresh_token": refresh})
    assert reused.status_code == 401 and reused.json()["error"] == "refresh_token_reused"
    newer = await client.post("/api/v1/admin/auth/refresh", json={"refresh_token": rotated.json()["tokens"]["refresh_token"]})
    assert newer.status_code == 401
    # An app refresh token can't be swapped for an admin session.
    app_login = (await client.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})).json()
    swapped = await client.post("/api/v1/admin/auth/refresh", json={"refresh_token": app_login["tokens"]["refresh_token"]})
    assert swapped.status_code == 401


# ─── Disabling users ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabling_someone_ends_their_access_until_enabled(client, admin, test_user, auth_headers, db_session):
    session = (await sign_in(client))["session"]
    assert (await client.get("/api/v1/auth/me", headers=auth_headers)).status_code == 200

    no_reason = await client.post(f"/api/v1/admin/users/{test_user.id}/disable", json={"reason": ""}, headers=bearer(session))
    assert no_reason.status_code == 422
    done = await client.post(f"/api/v1/admin/users/{test_user.id}/disable", json={"reason": "Fraud report #12"}, headers=bearer(session))
    assert done.status_code == 200 and done.json()["disabledAt"]

    blocked = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert blocked.status_code == 403 and blocked.json()["error"] == "account_disabled"
    login = await client.post("/api/v1/auth/login", json={"email": "test@example.com", "password": "testpass123"})
    assert login.status_code == 403 and login.json()["error"] == "account_disabled"
    left = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == test_user.id))).scalars().all()
    assert left == []

    back = await client.post(f"/api/v1/admin/users/{test_user.id}/enable", json={"reason": "Cleared after review"}, headers=bearer(session))
    assert back.status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=auth_headers)).status_code == 200

    log = (await client.get("/api/v1/admin/audit", params={"targetId": str(test_user.id)}, headers=bearer(session))).json()
    assert [e["action"] for e in log["entries"]] == ["user.enable", "user.disable"]
    assert log["entries"][1]["reason"] == "Fraud report #12" and log["entries"][1]["actorName"] == "Ops Admin"


@pytest.mark.asyncio
async def test_admins_cant_be_disabled_directly(client, admin):
    session = (await sign_in(client))["session"]
    resp = await client.post(f"/api/v1/admin/users/{admin.id}/disable", json={"reason": "testing"}, headers=bearer(session))
    assert resp.status_code == 409 and resp.json()["error"] == "is_admin"


# ─── Team ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_granting_and_removing_admins(client, admin, test_user, db_session):
    session = (await sign_in(client))["session"]
    granted = await client.post("/api/v1/admin/team", json={"email": "TEST@example.com", "reason": "New support hire"}, headers=bearer(session))
    assert granted.status_code == 200
    team = (await client.get("/api/v1/admin/team", headers=bearer(session))).json()["admins"]
    assert {a["email"] for a in team} == {ADMIN_EMAIL, "test@example.com"}

    missing = await client.post("/api/v1/admin/team", json={"email": "nobody@example.com", "reason": "x" * 3}, headers=bearer(session))
    assert missing.status_code == 404
    self_remove = await client.request("DELETE", f"/api/v1/admin/team/{admin.id}", json={"reason": "leaving"}, headers=bearer(session))
    assert self_remove.status_code == 409

    removed = await client.request("DELETE", f"/api/v1/admin/team/{test_user.id}", json={"reason": "Contract ended"}, headers=bearer(session))
    assert removed.status_code == 200
    await db_session.refresh(test_user)
    assert test_user.platform_role is None


@pytest.mark.asyncio
async def test_resetting_two_step_means_setting_it_up_again(client, admin):
    session = (await sign_in(client))["session"]
    reset = await client.post(f"/api/v1/admin/team/{admin.id}/reset-two-step", json={"reason": "Lost phone"}, headers=bearer(session))
    assert reset.status_code == 200 and reset.json()["twoStepSince"] is None
    again = (await client.post("/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})).json()
    assert again["step"] == "enroll"


@pytest.mark.asyncio
async def test_audit_log_pages_newest_first(client, admin, test_user):
    session = (await sign_in(client))["session"]
    for i in range(3):
        await client.post(f"/api/v1/admin/users/{test_user.id}/sign-out", json={"reason": f"round {i}"}, headers=bearer(session))
    first = (await client.get("/api/v1/admin/audit", params={"action": "user.", "limit": 2}, headers=bearer(session))).json()
    assert [e["reason"] for e in first["entries"]] == ["round 2", "round 1"] and first["nextCursor"]
    rest = (await client.get("/api/v1/admin/audit", params={"action": "user.", "limit": 2, "cursor": first["nextCursor"]},
                             headers=bearer(session))).json()
    assert [e["reason"] for e in rest["entries"]] == ["round 0"] and rest["nextCursor"] is None
