import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.business.models import Staff
from tests.test_create_booking import NBO, body, book, customer, day, salon, set_deposit  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, insert_booking, make_user


def owner_body(salon, staff=None, services=(("cut", 1),), **extra):
    return {
        "businessId": salon["id"],
        "services": [{"serviceId": salon["services"][n], "quantity": q} for n, q in services],
        "staffId": salon[staff] if staff else None,
        **extra,
    }


async def listing(client, headers, salon, **params):
    resp = await client.get(f"/api/v1/businesses/{salon['id']}/bookings", params=params, headers=headers)
    return resp


# ─── Calendar list ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_day_and_filters(client, auth_headers, salon, customer):
    _, cust = customer
    assigned = (await book(client, cust, body(salon, time="10:00"))).json()["bookingId"]
    anyone = (await book(client, cust, body(salon, time="11:00", staff=None))).json()["bookingId"]

    resp = await listing(client, auth_headers, salon, date=day().isoformat())
    assert resp.status_code == 200, resp.text
    assert [b["bookingId"] for b in resp.json()["bookings"]] == [assigned, anyone]  # soonest first

    ids = lambda r: [b["bookingId"] for b in r.json()["bookings"]]  # noqa: E731
    assert ids(await listing(client, auth_headers, salon, date=day().isoformat(), unassigned="true")) == [anyone]
    assert ids(await listing(client, auth_headers, salon, date=day().isoformat(), staffId=salon["a"])) == [assigned]
    assert ids(await listing(client, auth_headers, salon, date=day().isoformat(), status="cancelled")) == []
    week = await listing(client, auth_headers, salon, **{"from": (day() - timedelta(days=3)).isoformat(), "to": day().isoformat()})
    assert ids(week) == [assigned, anyone]
    other_day = await listing(client, auth_headers, salon, date=(day() + timedelta(days=1)).isoformat())
    assert ids(other_day) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, {"from": "2026-10-01"}, {"from": "2026-10-10", "to": "2026-10-01"},
                                    {"from": "2026-10-01", "to": "2026-12-31"}, {"date": "2026-10-01", "status": "bogus"}])
async def test_list_validation(client, auth_headers, salon, params):
    assert (await listing(client, auth_headers, salon, **params)).status_code == 400


@pytest.mark.asyncio
async def test_list_access(client, salon, customer, db_session):
    user, cust = customer
    mine = (await book(client, cust, body(salon, time="10:00", staff="a"))).json()["bookingId"]
    await book(client, cust, body(salon, time="10:00", staff="b"))

    assert (await listing(client, cust, salon, date=day().isoformat())).status_code == 404  # a customer

    stylist = await make_user(db_session, "wanjiku.staff@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id = stylist.id
    await db_session.commit()
    seen = await listing(client, headers_for(stylist), salon, date=day().isoformat(), staffId=salon["b"])
    assert [b["bookingId"] for b in seen.json()["bookings"]] == [mine]  # staff only ever see their own


# ─── Walk-ins ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_walk_in_starts_now_in_progress(client, auth_headers, salon):
    before = datetime.now(timezone.utc) - timedelta(minutes=1)
    resp = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, customerPhone="+254733000111"), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    b = resp.json()
    assert (b["status"], b["source"], b["customerName"], b["staffName"]) == ("in_progress", "walk_in", "Walk-in customer", "Any available")
    assert (b["depositAmount"], b["totalAmount"]["amount"], b["customerPhone"]) == (None, 800, "+254733000111")
    assert before <= datetime.fromisoformat(b["startsAt"]) <= datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_walk_in_with_busy_staff(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) - timedelta(minutes=10))  # A is mid-appointment
    busy = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="a"), headers=auth_headers)
    assert busy.status_code == 409 and busy.json()["error"] == "slot_unavailable"
    ok = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="b", customerName="Baraka"), headers=auth_headers)
    assert ok.status_code == 201 and ok.json()["customerName"] == "Baraka"


@pytest.mark.asyncio
async def test_only_managers_record_walk_ins(client, salon, customer):
    _, cust = customer
    resp = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon), headers=cust)
    assert resp.status_code == 404  # not their business


# ─── Scheduled on a customer's behalf ────────────────────────


@pytest.mark.asyncio
async def test_scheduled_booking(client, auth_headers, salon):
    await set_deposit(client, auth_headers, salon, {"required": True, "type": "fixed", "amount": {"amount": 300, "currency": "KES"}, "appliesTo": "all_services"})
    resp = await client.post(
        "/api/v1/bookings/scheduled",
        json=owner_body(salon, staff="a", customerName="Jane Doe", date=day().isoformat(), time="14:05"),  # off-grid is fine
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    b = resp.json()
    assert (b["status"], b["source"], b["time"], b["depositAmount"]) == ("confirmed", "walk_in", "14:05", None)
    # It blocks the slot for customers.
    slots = await client.get(f"/api/v1/businesses/{salon['id']}/availability",
                             params={"date": day().isoformat(), "serviceIds": salon["services"]["cut"], "staffId": salon["a"]})
    assert "14:00" not in [s["time"] for s in slots.json()["slots"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("when,status", [("08:00", 400), ("16:30", 400)])  # before opening; would end after close
async def test_scheduled_outside_hours(client, auth_headers, salon, when, status):
    resp = await client.post("/api/v1/bookings/scheduled", json=owner_body(salon, date=day().isoformat(), time=when), headers=auth_headers)
    assert resp.status_code == status


@pytest.mark.asyncio
async def test_scheduled_in_the_past_or_clashing(client, auth_headers, salon, customer):
    _, cust = customer
    past = await client.post("/api/v1/bookings/scheduled",
                             json=owner_body(salon, date=(datetime.now(NBO) - timedelta(days=1)).date().isoformat(), time="10:00"),
                             headers=auth_headers)
    assert past.status_code == 400
    await book(client, cust, body(salon, time="10:00", staff="a"))
    clash = await client.post("/api/v1/bookings/scheduled", json=owner_body(salon, staff="a", date=day().isoformat(), time="10:15"), headers=auth_headers)
    assert clash.status_code == 409


# ─── Status changes ──────────────────────────────────────────


async def set_status(client, headers, booking_id, status, **extra):
    return await client.patch(f"/api/v1/bookings/{booking_id}/status", json={"status": status, **extra}, headers=headers)


@pytest.mark.asyncio
async def test_check_in_complete_and_invalid_moves(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(hours=2))
    assert (await set_status(client, auth_headers, b.id, "in_progress")).json()["status"] == "in_progress"
    assert (await set_status(client, auth_headers, b.id, "completed")).json()["status"] == "completed"
    bad = await set_status(client, auth_headers, b.id, "in_progress")
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_status_transition"


@pytest.mark.asyncio
async def test_no_show_only_after_the_start(client, auth_headers, salon, customer, db_session):
    user, _ = customer
    future = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(hours=2))
    assert (await set_status(client, auth_headers, future.id, "no_show")).status_code == 400
    missed = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) - timedelta(hours=1))
    assert (await set_status(client, auth_headers, missed.id, "no_show")).json()["status"] == "no_show"


@pytest.mark.asyncio
async def test_business_cancel(client, auth_headers, salon, customer):
    _, cust = customer
    booking_id = (await book(client, cust, body(salon))).json()["bookingId"]
    b = (await set_status(client, auth_headers, booking_id, "cancelled")).json()
    assert (b["status"], b["cancelReason"]) == ("cancelled", "business")
    # The customer sees it under Cancelled.
    assert [x["bookingId"] for x in (await client.get("/api/v1/me/bookings", params={"filter": "cancelled"}, headers=cust)).json()["bookings"]] == [booking_id]


@pytest.mark.asyncio
async def test_staff_can_move_their_own_booking_along_only(client, salon, customer, db_session):
    user, cust = customer
    stylist = await make_user(db_session, "a.stylist@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id = stylist.id
    await db_session.commit()
    mine = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(hours=2))
    h = headers_for(stylist)
    assert (await set_status(client, h, mine.id, "cancelled")).status_code == 403
    assert (await set_status(client, h, mine.id, "in_progress")).status_code == 200
    assert (await set_status(client, cust, mine.id, "completed")).status_code == 404  # the customer isn't staff
