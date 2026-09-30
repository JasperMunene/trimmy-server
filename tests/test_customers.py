import uuid

import pytest

from app.modules.business.models import Staff
from tests.test_business_bookings import owner_body
from tests.test_create_booking import body, book, customer, day, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


async def customers(client, headers, salon, **params):
    resp = await client.get(f"/api/v1/businesses/{salon['id']}/customers", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def save(client, headers, salon, **json):
    return await client.post(f"/api/v1/businesses/{salon['id']}/customers", json=json, headers=headers)


@pytest.mark.asyncio
async def test_bookings_fill_the_list_deduplicated_by_phone(client, auth_headers, salon, customer):
    _, cust = customer
    await book(client, cust, body(salon, time="10:00"))  # Amina W, +254722000111 (from her account)
    await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, customerName="Amina", customerPhone="0722 000 111"), headers=auth_headers)
    await client.post("/api/v1/bookings/scheduled",
                      json=owner_body(salon, customerName="Baraka", customerPhone="0733000222", date=day().isoformat(), time="15:00"),
                      headers=auth_headers)
    await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="b"), headers=auth_headers)  # no phone: not listed

    data = await customers(client, auth_headers, salon)
    assert data["total"] == 2
    by_phone = {c["phone"]: c for c in data["customers"]}
    assert set(by_phone) == {"+254722000111", "+254733000222"}  # "0722 000 111" matched the same person
    assert (by_phone["+254722000111"]["name"], by_phone["+254722000111"]["bookingsCount"]) == ("Amina", 2)
    assert by_phone["+254733000222"]["lastBookingAt"].startswith(day().isoformat())


@pytest.mark.asyncio
async def test_save_creates_then_updates_by_phone(client, auth_headers, salon):
    created = await save(client, auth_headers, salon, name="Jane Doe", phone="0798765432", email="jane@example.com")
    assert created.status_code == 200, created.text
    c = created.json()
    assert (c["name"], c["phone"], c["email"], c["bookingsCount"]) == ("Jane Doe", "+254798765432", "jane@example.com", 0)

    again = (await save(client, auth_headers, salon, name="Jane W. Doe", phone="+254 798 765 432", email=None)).json()
    assert again["customerId"] == c["customerId"]  # same phone: same customer
    assert (again["name"], again["email"]) == ("Jane W. Doe", None)

    by_id = (await save(client, auth_headers, salon, customerId=c["customerId"], name="Jane", phone="0711111222")).json()
    assert (by_id["customerId"], by_id["phone"]) == (c["customerId"], "+254711111222")
    assert (await customers(client, auth_headers, salon))["total"] == 1


@pytest.mark.asyncio
async def test_customers_without_a_phone_can_be_saved(client, auth_headers, salon):
    a = (await save(client, auth_headers, salon, name="No Phone")).json()
    b = (await save(client, auth_headers, salon, name="No Phone")).json()
    assert a["phone"] is None and a["customerId"] != b["customerId"]  # nothing to deduplicate on


@pytest.mark.asyncio
async def test_phone_clash_and_bad_input(client, auth_headers, salon):
    one = (await save(client, auth_headers, salon, name="One", phone="0700111222")).json()
    await save(client, auth_headers, salon, name="Two", phone="0700333444")
    clash = await save(client, auth_headers, salon, customerId=one["customerId"], name="One", phone="0700333444")
    assert clash.status_code == 409 and clash.json()["error"] == "phone_taken"
    assert (await save(client, auth_headers, salon, name="Bad", phone="12")).json()["error"] == "invalid_phone"
    assert (await save(client, auth_headers, salon, name="")).status_code == 422
    assert (await save(client, auth_headers, salon, customerId=str(uuid.uuid4()), name="Ghost")).status_code == 404


@pytest.mark.asyncio
async def test_search_and_paging(client, auth_headers, salon):
    for name, phone in (("Amina", "0711000001"), ("Baraka", "0711000002"), ("Chebet", "0722999003")):
        await save(client, auth_headers, salon, name=name, phone=phone)
    assert [c["name"] for c in (await customers(client, auth_headers, salon, query="bar"))["customers"]] == ["Baraka"]
    assert [c["name"] for c in (await customers(client, auth_headers, salon, query="0722"))["customers"]] == ["Chebet"]
    page = await customers(client, auth_headers, salon, limit=2, offset=2)
    assert (page["total"], [c["name"] for c in page["customers"]]) == (3, ["Chebet"])


@pytest.mark.asyncio
async def test_who_can_see_customers(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    url = f"/api/v1/businesses/{salon['id']}/customers"
    assert (await client.get(url, headers=cust)).status_code == 404
    stylist = await make_user(db_session, "list.staff@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id = stylist.id
    await db_session.commit()
    assert (await client.get(url, headers=headers_for(stylist))).status_code == 403


# ─── One customer's bookings (Customer Detail) ───────────────


async def history(client, headers, salon, **params):
    return await client.get(f"/api/v1/businesses/{salon['id']}/bookings", params=params, headers=headers)


@pytest.mark.asyncio
async def test_bookings_by_customer_newest_first_any_date(client, auth_headers, salon, customer, db_session):
    from datetime import datetime, timedelta, timezone

    from tests.test_read_bookings import insert_booking

    user, _ = customer
    now = datetime.now(timezone.utc)
    old = await insert_booking(db_session, salon, user.id, now - timedelta(days=200), status="completed", customer_phone="+254722000111")
    soon = await insert_booking(db_session, salon, user.id, now + timedelta(days=3), customer_phone="+254722000111")
    await insert_booking(db_session, salon, user.id, now, customer_phone="+254733000222")  # someone else
    old_id, soon_id = str(old.id), str(soon.id)
    await save(client, auth_headers, salon, name="Amina", phone="+254722000111")
    (amina,) = [c for c in (await customers(client, auth_headers, salon))["customers"] if c["phone"] == "+254722000111"]

    by_id = await history(client, auth_headers, salon, customerId=amina["customerId"])
    assert by_id.status_code == 200, by_id.text
    assert [b["bookingId"] for b in by_id.json()["bookings"]] == [soon_id, old_id]
    by_phone = (await history(client, auth_headers, salon, phone="0722 000 111")).json()["bookings"]
    assert [b["bookingId"] for b in by_phone] == [soon_id, old_id]
    one = (await history(client, auth_headers, salon, phone="0722000111", limit=1)).json()["bookings"]
    assert [b["bookingId"] for b in one] == [soon_id]
    done = (await history(client, auth_headers, salon, phone="0722000111", status="completed")).json()["bookings"]
    assert [b["bookingId"] for b in done] == [old_id]


@pytest.mark.asyncio
async def test_bookings_by_customer_errors(client, auth_headers, salon):
    assert (await history(client, auth_headers, salon, customerId=str(uuid.uuid4()))).status_code == 404
    bad = await history(client, auth_headers, salon, phone="not a phone")
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_phone"
    no_phone = (await save(client, auth_headers, salon, name="Cash only")).json()
    assert (await history(client, auth_headers, salon, customerId=no_phone["customerId"])).json()["bookings"] == []
