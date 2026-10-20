import uuid
from datetime import timedelta

import pytest

from app.modules.business.models import Staff
from tests.test_create_booking import body, book, customer, day, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


def url(salon, staff="a"):
    return f"/api/v1/businesses/{salon['id']}/staff/{salon[staff]}/time-off"


async def slots(client, salon, staff="a"):
    r = await client.get(
        f"/api/v1/businesses/{salon['id']}/availability",
        params={"date": day().isoformat(), "serviceIds": salon["services"]["cut"], "staffId": salon[staff]},
    )
    assert r.status_code == 200, r.text
    return {s["time"] for s in r.json()["slots"]}


@pytest.mark.asyncio
async def test_time_off_hides_slots_and_blocks_booking(client, auth_headers, salon, customer):
    _, cust = customer
    assert "13:00" in await slots(client, salon)
    r = await client.post(url(salon), json={"date": day().isoformat(), "start": "12:30", "end": "15:00",
                                           "reason": "Doctor"}, headers=auth_headers)
    assert r.status_code == 201, r.text
    assert r.json()["start"] == "12:30" and r.json()["end"] == "15:00" and r.json()["allDay"] is False

    free = await slots(client, salon)
    assert "13:00" not in free and "12:00" not in free  # a 45-minute cut at 12:00 would run into it
    assert "15:00" in free and "11:30" in free
    assert "13:00" in await slots(client, salon, staff="b")  # only that person

    blocked = await book(client, cust, body(salon, time="13:00"))
    assert blocked.status_code == 409


@pytest.mark.asyncio
async def test_all_day_and_multi_day(client, auth_headers, salon):
    d = day()
    r = await client.post(url(salon), json={"date": d.isoformat(), "until": (d + timedelta(days=2)).isoformat()},
                          headers=auth_headers)
    assert r.status_code == 201, r.text
    assert r.json()["allDay"] is True and r.json()["end"] == "24:00" and r.json()["until"] == (d + timedelta(days=2)).isoformat()
    assert await slots(client, salon) == set()
    listed = await client.get(f"/api/v1/businesses/{salon['id']}/time-off",
                              params={"from": d.isoformat(), "to": d.isoformat()}, headers=auth_headers)
    assert len(listed.json()["timeOff"]) == 1


@pytest.mark.asyncio
async def test_cannot_cover_existing_bookings(client, auth_headers, salon, customer):
    _, cust = customer
    assert (await book(client, cust, body(salon, time="10:00"))).status_code == 201
    r = await client.post(url(salon), json={"date": day().isoformat(), "start": "09:00", "end": "11:00"},
                          headers=auth_headers)
    assert r.status_code == 409 and r.json()["error"] == "has_bookings" and len(r.json()["references"]) == 1


@pytest.mark.asyncio
async def test_who_can_change_it(client, auth_headers, salon, db_session):
    worker = await make_user(db_session, "timeoff.staff@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id = worker.id
    await db_session.commit()
    mine = headers_for(worker)
    payload = {"date": day().isoformat(), "start": "16:00", "end": "17:00"}

    own = await client.post(url(salon, "a"), json=payload, headers=mine)
    assert own.status_code == 201, own.text
    assert (await client.post(url(salon, "b"), json=payload, headers=mine)).status_code == 403  # someone else's
    stranger = headers_for(await make_user(db_session, "timeoff.stranger@example.com"))
    assert (await client.post(url(salon, "a"), json=payload, headers=stranger)).status_code == 404

    # Staff list only their own; the owner sees everyone's.
    await client.post(url(salon, "b"), json=payload, headers=auth_headers)
    q = {"from": day().isoformat(), "to": day().isoformat()}
    assert len((await client.get(f"/api/v1/businesses/{salon['id']}/time-off", params=q, headers=mine)).json()["timeOff"]) == 1
    assert len((await client.get(f"/api/v1/businesses/{salon['id']}/time-off", params=q, headers=auth_headers)).json()["timeOff"]) == 2

    tid = own.json()["timeOffId"]
    assert (await client.delete(f"/api/v1/time-off/{tid}", headers=stranger)).status_code == 404
    assert (await client.delete(f"/api/v1/time-off/{tid}", headers=mine)).status_code == 204


@pytest.mark.asyncio
async def test_validation(client, auth_headers, salon):
    d = day().isoformat()
    for bad in ({"date": d, "start": "15:00", "end": "14:00"}, {"date": d, "start": "15:00"},
                {"date": d, "start": "10:00", "end": "11:00", "until": d}):
        assert (await client.post(url(salon), json=bad, headers=auth_headers)).status_code == 422, bad
    past = {"date": "2020-01-01", "start": "10:00", "end": "11:00"}
    assert (await client.post(url(salon), json=past, headers=auth_headers)).status_code == 400
