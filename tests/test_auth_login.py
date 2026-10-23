import pytest


@pytest.mark.asyncio
async def test_login_success(client, test_user):
    response = await client.post("/api/v1/auth/login", json={
        "email": "test@example.com",
        "password": "testpass123",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["user"]["email"] == "test@example.com"
    assert data["user"]["verified"] is True
    assert "access_token" in data["tokens"]
    assert "refresh_token" in data["tokens"]


@pytest.mark.asyncio
async def test_login_wrong_password(client, test_user):
    response = await client.post("/api/v1/auth/login", json={
        "email": "test@example.com",
        "password": "wrongpassword",
    })
    assert response.status_code == 401
    data = response.json()
    assert data["error"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_login_nonexistent_user(client):
    response = await client.post("/api/v1/auth/login", json={
        "email": "nobody@example.com",
        "password": "password123",
    })
    assert response.status_code == 401
    data = response.json()
    assert data["error"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_login_unverified_user(client, unverified_user):
    response = await client.post("/api/v1/auth/login", json={
        "email": "unverified@example.com",
        "password": "testpass123",
    })
    assert response.status_code == 403
    data = response.json()
    assert data["error"] == "not_verified"


@pytest.mark.asyncio
async def test_too_many_wrong_passwords_lock_that_email_only(client, test_user, monkeypatch):
    from limits import parse
    from app.core import rate_limit

    monkeypatch.setattr(rate_limit, "_LOGIN_FAILURES", parse("3/15minutes"))
    wrong = {"email": "Test@Example.com", "password": "wrongpassword"}
    for _ in range(3):
        assert (await client.post("/api/v1/auth/login", json=wrong)).status_code == 401
    # Locked, even with the right password, and whatever the email's case.
    locked = await client.post("/api/v1/auth/login", json={"email": "test@example.com", "password": "testpass123"})
    assert locked.status_code == 429 and locked.json()["error"] == "too_many_attempts"
    # Other accounts aren't affected.
    other = await client.post("/api/v1/auth/login", json={"email": "someone.else@example.com", "password": "x"})
    assert other.status_code == 401


@pytest.mark.asyncio
async def test_a_good_login_clears_the_failures(client, test_user, monkeypatch):
    from limits import parse
    from app.core import rate_limit

    monkeypatch.setattr(rate_limit, "_LOGIN_FAILURES", parse("3/15minutes"))
    wrong = {"email": "test@example.com", "password": "wrongpassword"}
    right = {"email": "test@example.com", "password": "testpass123"}
    for _ in range(2):
        await client.post("/api/v1/auth/login", json=wrong)
    assert (await client.post("/api/v1/auth/login", json=right)).status_code == 200
    for _ in range(2):
        assert (await client.post("/api/v1/auth/login", json=wrong)).status_code == 401
    assert (await client.post("/api/v1/auth/login", json=right)).status_code == 200


@pytest.mark.asyncio
async def test_login_is_rate_limited_per_ip(client, test_user):
    from app.core.config import settings

    allowed = int(settings.RATE_LIMIT_LOGIN.split("/")[0])
    # A different email each time, so this is the IP limit and not one account's lock.
    for i in range(allowed):
        body = {"email": f"nobody{i}@example.com", "password": "x"}
        assert (await client.post("/api/v1/auth/login", json=body)).status_code == 401
    limited = await client.post("/api/v1/auth/login", json={"email": "next@example.com", "password": "x"})
    assert limited.status_code == 429
