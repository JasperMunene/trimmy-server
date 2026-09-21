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
