import uuid

import pytest

from app.modules.business.models import Business
from tests.test_create_booking import customer, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user

URL = "/api/v1/me/favorites"


@pytest.mark.asyncio
async def test_save_list_and_remove(client, salon, customer):
    _, h = customer
    assert (await client.get(URL, headers=h)).json() == {"businesses": [], "staff": []}
    for _ in range(2):  # idempotent
        assert (await client.put(f"{URL}/businesses/{salon['id']}", headers=h)).status_code == 204
    await client.put(f"{URL}/staff/{salon['a']}", headers=h)
    await client.put(f"{URL}/staff/{salon['b']}", headers=h)

    fav = (await client.get(URL, headers=h)).json()
    (biz,) = fav["businesses"]
    assert (biz["businessId"], biz["name"]) == (salon["id"], "Glow Beauty")
    assert biz["startingPrice"] == {"amount": 800, "currency": "KES"}  # search-result shape
    assert [(s["staffId"], s["businessName"]) for s in fav["staff"]] == [(salon["b"], "Glow Beauty"), (salon["a"], "Glow Beauty")]

    assert (await client.delete(f"{URL}/businesses/{salon['id']}", headers=h)).status_code == 204
    assert (await client.delete(f"{URL}/businesses/{salon['id']}", headers=h)).status_code == 204
    await client.delete(f"{URL}/staff/{salon['a']}", headers=h)
    fav = (await client.get(URL, headers=h)).json()
    assert fav["businesses"] == [] and [s["staffId"] for s in fav["staff"]] == [salon["b"]]


@pytest.mark.asyncio
async def test_favorites_are_per_user(client, salon, customer, db_session):
    _, h = customer
    await client.put(f"{URL}/businesses/{salon['id']}", headers=h)
    other = headers_for(await make_user(db_session, "other.fav@example.com"))
    assert (await client.get(URL, headers=other)).json()["businesses"] == []


@pytest.mark.asyncio
async def test_unknown_or_unpublished(client, salon, customer, db_session):
    _, h = customer
    assert (await client.put(f"{URL}/businesses/{uuid.uuid4()}", headers=h)).status_code == 404
    assert (await client.put(f"{URL}/staff/not-a-uuid", headers=h)).status_code == 404
    await client.put(f"{URL}/businesses/{salon['id']}", headers=h)
    await client.put(f"{URL}/staff/{salon['a']}", headers=h)
    business = await db_session.get(Business, uuid.UUID(salon["id"]))
    business.status = "draft"
    await db_session.commit()
    assert (await client.get(URL, headers=h)).json() == {"businesses": [], "staff": []}  # hidden, not lost
    assert (await client.put(f"{URL}/businesses/{salon['id']}", headers=h)).status_code == 404
    assert (await client.get(URL)).status_code == 401
