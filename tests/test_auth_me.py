import pytest


@pytest.mark.asyncio
async def test_get_me_success(client, test_user, auth_headers):
    response = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["first_name"] == "Test"
    assert data["verified"] is True


@pytest.mark.asyncio
async def test_get_me_no_token(client):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_invalid_token(client):
    response = await client.get("/api/v1/auth/me", headers={
        "Authorization": "Bearer invalid-token-here"
    })
    assert response.status_code == 401
