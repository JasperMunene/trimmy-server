import pytest


async def _business(client, headers):
    resp = await client.post(
        "/api/v1/businesses",
        json={"name": "Glow", "categories": ["hair_salon"], "phone": "+254700661122",
              "location": {"address": "Kilimani", "lat": -1.29, "lng": 36.78}},
        headers=headers,
    )
    return resp.json()["businessId"]


@pytest.mark.asyncio
async def test_defaults_show_on_business(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    settings = (await client.get(f"/api/v1/businesses/{biz_id}", headers=auth_headers)).json()["bookingSettings"]
    assert settings == {
        "businessId": biz_id, "timezone": "Africa/Nairobi", "slotIntervalMinutes": 15,
        "bufferMinutes": 0, "minLeadMinutes": 30, "maxDaysAhead": 30,
    }


@pytest.mark.asyncio
async def test_partial_update(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/booking-settings",
        json={"slotIntervalMinutes": 30, "bufferMinutes": 10},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert (data["slotIntervalMinutes"], data["bufferMinutes"], data["minLeadMinutes"]) == (30, 10, 30)

    again = (await client.get(f"/api/v1/businesses/{biz_id}", headers=auth_headers)).json()["bookingSettings"]
    assert again == data


@pytest.mark.asyncio
async def test_timezone(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    ok = await client.patch(f"/api/v1/businesses/{biz_id}/booking-settings", json={"timezone": "Africa/Kampala"}, headers=auth_headers)
    assert ok.json()["timezone"] == "Africa/Kampala"
    bad = await client.patch(f"/api/v1/businesses/{biz_id}/booking-settings", json={"timezone": "Mars/Olympus"}, headers=auth_headers)
    assert bad.status_code == 400
    assert bad.json()["error"] == "validation_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"slotIntervalMinutes": 25}, 400),  # doesn't divide 60
    ({"slotIntervalMinutes": 3}, 422),
    ({"slotIntervalMinutes": 90}, 422),
    ({"bufferMinutes": -5}, 422),
    ({"maxDaysAhead": 0}, 422),
    ({"minLeadMinutes": None}, 400),
])
async def test_rejects_invalid(client, auth_headers, body, status):
    biz_id = await _business(client, auth_headers)
    resp = await client.patch(f"/api/v1/businesses/{biz_id}/booking-settings", json=body, headers=auth_headers)
    assert resp.status_code == status


@pytest.mark.asyncio
async def test_other_owner_cannot_change(client, auth_headers):
    resp = await client.patch(
        "/api/v1/businesses/00000000-0000-0000-0000-000000000000/booking-settings",
        json={"bufferMinutes": 5}, headers=auth_headers,
    )
    assert resp.status_code == 404
