import pytest


@pytest.mark.asyncio
async def test_refresh_success(client, test_user):
    login_response = await client.post("/api/v1/auth/login", json={
        "email": "test@example.com",
        "password": "testpass123",
    })
    refresh_token = login_response.json()["tokens"]["refresh_token"]

    response = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": refresh_token,
    })
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_refresh_invalid_token(client):
    response = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": "invalid-token",
    })
    assert response.status_code == 401
    data = response.json()
    assert data["error"] == "invalid_refresh_token"


@pytest.mark.asyncio
async def test_refresh_access_token_rejected(client, test_user):
    login_response = await client.post("/api/v1/auth/login", json={
        "email": "test@example.com",
        "password": "testpass123",
    })
    access_token = login_response.json()["tokens"]["access_token"]

    response = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": access_token,
    })
    assert response.status_code == 401
