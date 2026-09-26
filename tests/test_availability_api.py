import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.business.models import Business, Staff

ALL_WEEK = {d: {"open": "09:00", "close": "17:00"} for d in
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}


def next_week():
    return (datetime.now(ZoneInfo("Africa/Nairobi")) + timedelta(days=7)).date()


@pytest.fixture
async def salon(client, auth_headers, db_session):
    biz = await client.post(
        "/api/v1/businesses",
        json={"name": "Glow", "categories": ["hair_salon"], "phone": "+254700771122",
              "location": {"address": "Kilimani", "lat": -1.29, "lng": 36.78}},
        headers=auth_headers,
    )
    biz_id = biz.json()["businessId"]
    await client.put(f"/api/v1/businesses/{biz_id}/working-hours", json={"weeklyHours": ALL_WEEK}, headers=auth_headers)
    cat = (await client.post(f"/api/v1/businesses/{biz_id}/service-categories", json={"name": "Hair"}, headers=auth_headers)).json()
    services = {}
    for name, minutes in (("cut", 45), ("colour", 90)):
        svc = await client.post(
            f"/api/v1/businesses/{biz_id}/services",
            json={"categoryId": cat["categoryId"], "name": name, "durationMinutes": minutes, "price": {"amount": 800, "currency": "KES"}},
            headers=auth_headers,
        )
        services[name] = svc.json()["serviceId"]

    wanjiku = Staff(id=uuid.uuid4(), business_id=uuid.UUID(biz_id), name="Wanjiku", role="staff")
    otieno = Staff(id=uuid.uuid4(), business_id=uuid.UUID(biz_id), name="Otieno", role="staff",
                   working_days=["monday"], performs_all_services=True)
    db_session.add_all([wanjiku, otieno])
    business = await db_session.get(Business, uuid.UUID(biz_id))
    business.status = "published"
    await db_session.commit()
    return {"id": biz_id, "services": services, "wanjiku": str(wanjiku.id), "otieno": str(otieno.id)}


async def get_day(client, salon, day, services=("cut",), **params):
    query = [("date", day.isoformat())] + [("serviceIds", salon["services"][s]) for s in services]
    query += list(params.items())
    return await client.get(f"/api/v1/businesses/{salon['id']}/availability", params=query)


@pytest.mark.asyncio
async def test_day_slots(client, salon):
    day = next_week()
    resp = await get_day(client, salon, day)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["date"] == day.isoformat()
    assert data["timezone"] == "Africa/Nairobi"
    assert data["durationMinutes"] == 45
    first = data["slots"][0]
    assert first["time"] == "09:00"
    assert first["startsAt"].startswith(f"{day.isoformat()}T09:00:00") and first["startsAt"].endswith("+03:00")
    assert salon["wanjiku"] in first["staffIds"]
    assert data["slots"][-1]["time"] == "16:15"  # 45 min must end by 17:00


@pytest.mark.asyncio
async def test_duration_sums_services(client, salon):
    data = (await get_day(client, salon, next_week(), services=("cut", "colour"))).json()
    assert data["durationMinutes"] == 135
    assert data["slots"][-1]["time"] == "14:45"


@pytest.mark.asyncio
async def test_specific_staff_and_working_days(client, salon):
    day = next_week()
    data = (await get_day(client, salon, day, staffId=salon["otieno"])).json()
    if day.weekday() == 0:
        assert data["slots"] and all(s["staffIds"] == [salon["otieno"]] for s in data["slots"])
    else:
        assert data["slots"] == []  # Otieno only works Mondays


@pytest.mark.asyncio
async def test_staff_who_cannot_perform(client, salon, db_session):
    member = await db_session.get(Staff, uuid.UUID(salon["wanjiku"]))
    member.performs_all_services = False
    await db_session.commit()
    resp = await get_day(client, salon, next_week(), staffId=salon["wanjiku"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "staff_cannot_perform"


@pytest.mark.asyncio
async def test_days_strip(client, salon):
    params = [("serviceIds", salon["services"]["cut"]), ("days", 7)]
    resp = await client.get(f"/api/v1/businesses/{salon['id']}/availability/days", params=params)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["days"]) == 7
    assert all(d["isOpen"] for d in data["days"])
    assert data["days"][-1]["hasSlots"] is True
    assert data["days"][0]["date"] == datetime.now(ZoneInfo("Africa/Nairobi")).date().isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, 32])
async def test_days_range_limit(client, salon, days):
    params = [("serviceIds", salon["services"]["cut"]), ("days", days)]
    resp = await client.get(f"/api/v1/businesses/{salon['id']}/availability/days", params=params)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_validation(client, salon):
    day = next_week().isoformat()
    base = f"/api/v1/businesses/{salon['id']}/availability"
    assert (await client.get(base, params={"date": day})).status_code == 422  # serviceIds required
    foreign = await client.get(base, params={"date": day, "serviceIds": str(uuid.uuid4())})
    assert foreign.status_code == 400 and foreign.json()["error"] == "validation_error"
    unknown_staff = await client.get(base, params={"date": day, "serviceIds": salon["services"]["cut"], "staffId": str(uuid.uuid4())})
    assert unknown_staff.status_code == 404


@pytest.mark.asyncio
async def test_draft_business_is_404(client, salon, db_session):
    business = await db_session.get(Business, uuid.UUID(salon["id"]))
    business.status = "draft"
    await db_session.commit()
    assert (await get_day(client, salon, next_week())).status_code == 404


@pytest.mark.asyncio
async def test_uses_booking_settings(client, auth_headers, salon):
    await client.patch(f"/api/v1/businesses/{salon['id']}/booking-settings", json={"slotIntervalMinutes": 30}, headers=auth_headers)
    times = [s["time"] for s in (await get_day(client, salon, next_week())).json()["slots"]]
    assert times[:3] == ["09:00", "09:30", "10:00"]
