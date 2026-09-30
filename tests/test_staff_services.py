import uuid

import pytest

from app.modules.business.models import Business, Staff


async def _setup(client, headers, db_session):
    biz = await client.post(
        "/api/v1/businesses",
        json={"name": "Glow", "categories": ["hair_salon"], "phone": "+254700554433",
              "location": {"address": "Kilimani", "lat": -1.29, "lng": 36.78}},
        headers=headers,
    )
    biz_id = biz.json()["businessId"]
    cat = await client.post(f"/api/v1/businesses/{biz_id}/service-categories", json={"name": "Hair"}, headers=headers)
    services = []
    for name in ("Cut", "Colour"):
        svc = await client.post(
            f"/api/v1/businesses/{biz_id}/services",
            json={"categoryId": cat.json()["categoryId"], "name": name, "durationMinutes": 30, "price": {"amount": 500, "currency": "KES"}},
            headers=headers,
        )
        services.append(svc.json()["serviceId"])
    staff = Staff(id=uuid.uuid4(), business_id=uuid.UUID(biz_id), name="Wanjiku", role="staff")
    db_session.add(staff)
    await db_session.commit()
    return biz_id, str(staff.id), services, cat.json()["categoryId"]


def _url(biz_id, staff_id):
    return f"/api/v1/businesses/{biz_id}/staff/{staff_id}/services"


@pytest.mark.asyncio
async def test_defaults_to_all_services(client, auth_headers, db_session):
    biz_id, staff_id, _, _ = await _setup(client, auth_headers, db_session)
    resp = await client.get(_url(biz_id, staff_id), headers=auth_headers)
    assert resp.json() == {"staffId": staff_id, "allServices": True, "serviceIds": []}


@pytest.mark.asyncio
async def test_set_specific_services_and_back(client, auth_headers, db_session):
    biz_id, staff_id, (cut, colour), _ = await _setup(client, auth_headers, db_session)

    resp = await client.put(_url(biz_id, staff_id), json={"allServices": False, "serviceIds": [cut, cut]}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"staffId": staff_id, "allServices": False, "serviceIds": [cut]}

    business = await db_session.get(Business, uuid.UUID(biz_id))
    business.status = "published"
    await db_session.commit()
    profile = (await client.get(f"/api/v1/businesses/{biz_id}/profile")).json()
    (listed,) = [s for s in profile["staff"] if s["staffId"] == staff_id]
    assert (listed["allServices"], listed["serviceIds"]) == (False, [cut])

    resp = await client.put(_url(biz_id, staff_id), json={"allServices": True}, headers=auth_headers)
    assert resp.json() == {"staffId": staff_id, "allServices": True, "serviceIds": []}


@pytest.mark.asyncio
async def test_performs_nothing_is_allowed(client, auth_headers, db_session):
    biz_id, staff_id, _, _ = await _setup(client, auth_headers, db_session)
    resp = await client.put(_url(biz_id, staff_id), json={"allServices": False, "serviceIds": []}, headers=auth_headers)
    assert resp.json() == {"staffId": staff_id, "allServices": False, "serviceIds": []}


@pytest.mark.asyncio
async def test_deleting_the_last_mapped_service_leaves_staff_performing_nothing(client, auth_headers, db_session):
    biz_id, staff_id, (cut, _), cat_id = await _setup(client, auth_headers, db_session)
    await client.put(_url(biz_id, staff_id), json={"allServices": False, "serviceIds": [cut]}, headers=auth_headers)
    await client.delete(f"/api/v1/businesses/{biz_id}/services/{cut}", headers=auth_headers)

    resp = await client.get(_url(biz_id, staff_id), headers=auth_headers)
    # Not flipped back to "performs everything".
    assert resp.json() == {"staffId": staff_id, "allServices": False, "serviceIds": []}


@pytest.mark.asyncio
async def test_deleting_a_category_drops_its_mappings(client, auth_headers, db_session):
    biz_id, staff_id, (cut, colour), cat_id = await _setup(client, auth_headers, db_session)
    await client.put(_url(biz_id, staff_id), json={"allServices": False, "serviceIds": [cut, colour]}, headers=auth_headers)
    await client.delete(f"/api/v1/businesses/{biz_id}/service-categories/{cat_id}", headers=auth_headers)
    assert (await client.get(_url(biz_id, staff_id), headers=auth_headers)).json()["serviceIds"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"allServices": True, "serviceIds": ["__cut__"]},
    {"allServices": False, "serviceIds": [str(uuid.uuid4())]},
])
async def test_rejects_invalid(client, auth_headers, db_session, body):
    biz_id, staff_id, (cut, _), _ = await _setup(client, auth_headers, db_session)
    body = {**body, "serviceIds": [cut if i == "__cut__" else i for i in body["serviceIds"]]}
    resp = await client.put(_url(biz_id, staff_id), json=body, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_other_business_staff_is_404(client, auth_headers, db_session):
    biz_id, _, _, _ = await _setup(client, auth_headers, db_session)
    stranger = Staff(id=uuid.uuid4(), business_id=uuid.uuid4(), name="X", role="staff")
    db_session.add(stranger)
    await db_session.commit()
    resp = await client.get(_url(biz_id, stranger.id), headers=auth_headers)
    assert resp.status_code == 404


# ─── The staff member's own view (team.json#get-my-staff-services) ─


@pytest.mark.asyncio
async def test_staff_see_their_own_services(client, auth_headers, db_session):
    from tests.test_read_bookings import headers_for, make_user

    biz_id, staff_id, (cut, colour), _ = await _setup(client, auth_headers, db_session)
    user = await make_user(db_session, "my.services@example.com")
    member = await db_session.get(Staff, uuid.UUID(staff_id))
    member.user_id = user.id
    await db_session.commit()
    mine = lambda: client.get("/api/v1/me/staff-services", params={"businessId": biz_id}, headers=headers_for(user))  # noqa: E731

    everything = (await mine()).json()
    assert (everything["staffId"], everything["allServices"]) == (staff_id, True)
    assert sorted(s["name"] for s in everything["services"]) == ["Colour", "Cut"]

    await client.put(_url(biz_id, staff_id), json={"allServices": False, "serviceIds": [cut]}, headers=auth_headers)
    only_cut = (await mine()).json()
    assert only_cut["allServices"] is False
    assert only_cut["services"] == [{"serviceId": cut, "name": "Cut", "durationMinutes": 30, "price": {"amount": 500, "currency": "KES"}}]

    await client.put(_url(biz_id, staff_id), json={"allServices": False, "serviceIds": []}, headers=auth_headers)
    assert (await mine()).json()["services"] == []


@pytest.mark.asyncio
async def test_my_staff_services_elsewhere_is_404(client, auth_headers, db_session):
    biz_id, _, _, _ = await _setup(client, auth_headers, db_session)
    resp = await client.get("/api/v1/me/staff-services", params={"businessId": biz_id}, headers=auth_headers)
    assert resp.status_code == 404  # the owner has no staff row here (team mode)
