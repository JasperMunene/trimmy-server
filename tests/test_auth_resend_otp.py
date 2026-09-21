import pytest


@pytest.mark.asyncio
async def test_resend_otp_verification(client, test_user):
    response = await client.post("/api/v1/auth/resend-otp", json={
        "email": "test@example.com",
        "purpose": "verification",
    })
    assert response.status_code == 200
    data = response.json()
    assert "message" in data


@pytest.mark.asyncio
async def test_resend_otp_password_reset(client, test_user):
    response = await client.post("/api/v1/auth/resend-otp", json={
        "email": "test@example.com",
        "purpose": "password_reset",
    })
    assert response.status_code == 200
    data = response.json()
    assert "message" in data


@pytest.mark.asyncio
async def test_resend_otp_nonexistent_user(client):
    response = await client.post("/api/v1/auth/resend-otp", json={
        "email": "nobody@example.com",
        "purpose": "verification",
    })
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_resend_otp_invalid_purpose(client, test_user):
    response = await client.post("/api/v1/auth/resend-otp", json={
        "email": "test@example.com",
        "purpose": "invalid",
    })
    assert response.status_code == 422
