import pytest


@pytest.mark.asyncio
async def test_forgot_password_existing_user(client, test_user):
    response = await client.post("/api/v1/auth/forgot-password", json={
        "email": "test@example.com",
    })
    assert response.status_code == 200
    data = response.json()
    assert "message" in data


@pytest.mark.asyncio
async def test_forgot_password_nonexistent_user(client):
    response = await client.post("/api/v1/auth/forgot-password", json={
        "email": "nobody@example.com",
    })
    assert response.status_code == 200
    data = response.json()
    assert "message" in data


@pytest.mark.asyncio
async def test_forgot_password_invalid_email(client):
    response = await client.post("/api/v1/auth/forgot-password", json={
        "email": "not-an-email",
    })
    assert response.status_code == 422
