import pytest


@pytest.mark.asyncio
async def test_register_success(client):
    response = await client.post("/api/v1/auth/register", json={
        "email": "new@example.com",
        "password": "password123",
        "first_name": "New",
        "last_name": "User",
    })
    assert response.status_code == 201
    data = response.json()
    assert data["user"]["email"] == "new@example.com"
    assert data["user"]["first_name"] == "New"
    assert data["user"]["verified"] is False
    assert "access_token" in data["tokens"]
    assert "refresh_token" in data["tokens"]


@pytest.mark.asyncio
async def test_register_with_phone(client):
    response = await client.post("/api/v1/auth/register", json={
        "email": "phone@example.com",
        "password": "password123",
        "first_name": "Phone",
        "last_name": "User",
        "phone": "+1234567890",
    })
    assert response.status_code == 201
    data = response.json()
    assert data["user"]["phone"] == "+1234567890"


@pytest.mark.asyncio
async def test_register_duplicate_email_unverified(client, unverified_user):
    response = await client.post("/api/v1/auth/register", json={
        "email": "unverified@example.com",
        "password": "newpassword123",
        "first_name": "New",
        "last_name": "Name",
    })
    assert response.status_code == 409
    data = response.json()
    assert data["error"] == "email_taken"
    assert "tokens" not in data


@pytest.mark.asyncio
async def test_register_duplicate_email_verified(client, test_user):
    response = await client.post("/api/v1/auth/register", json={
        "email": "test@example.com",
        "password": "password123",
        "first_name": "Dup",
        "last_name": "User",
    })
    assert response.status_code == 409
    data = response.json()
    assert data["error"] == "email_taken"


@pytest.mark.asyncio
async def test_register_invalid_email(client):
    response = await client.post("/api/v1/auth/register", json={
        "email": "not-an-email",
        "password": "password123",
        "first_name": "Bad",
        "last_name": "Email",
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_short_password(client):
    response = await client.post("/api/v1/auth/register", json={
        "email": "short@example.com",
        "password": "short",
        "first_name": "Short",
        "last_name": "Pass",
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_refresh_token_is_usable(client):
    response = await client.post("/api/v1/auth/register", json={
        "email": "fresh@example.com",
        "password": "password123",
        "first_name": "Fresh",
        "last_name": "User",
    })
    refresh_token = response.json()["tokens"]["refresh_token"]
    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert response.status_code == 200
