"""Visits: adding a service (maybe with someone else) to a customer's appointment, grouped so the
visit is checked out together."""
import uuid

import pytest

import app.modules.booking.tasks as booking_tasks
from app.modules.business.models import Staff
from tests.test_business_bookings import owner_body
from tests.test_create_booking import customer, day, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


async def _walk_in(client, headers, salon, staff="a", **extra):
    resp = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff=staff, customerName="Amina", customerPhone="0712345678", **extra), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _add(client, headers, booking_id, salon, staff="b", service="colour", **body):
    return await client.post(
        f"/api/v1/bookings/{booking_id}/add-service",
        json={"services": [{"serviceId": salon["services"][service], "quantity": 1}], "staffId": salon[staff], **body},
        headers=headers,
    )


@pytest.mark.asyncio
async def test_add_now_with_someone_else_makes_a_visit(client, auth_headers, salon):
    hair = await _walk_in(client, auth_headers, salon)
    resp = await _add(client, auth_headers, hair["bookingId"], salon, start="now")
    assert resp.status_code == 201, resp.text
    visit = resp.json()

    assert len(visit["bookings"]) == 2
    assert {b["staffId"] for b in visit["bookings"]} == {salon["a"], salon["b"]}
    assert all(b["visitId"] == visit["visitId"] for b in visit["bookings"])
    assert all(b["status"] == "in_progress" for b in visit["bookings"])  # both under way, side by side
    assert visit["customerName"] == "Amina"
    assert {b["customerPhone"] for b in visit["bookings"]} == {hair["customerPhone"]}

    assert visit["total"]["amount"] == 800 + 2500
    assert visit["balanceDue"]["amount"] == 3300
    assert visit["paymentStatus"] == "unpaid"
    per_staff = {s["staffId"]: s["subtotal"]["amount"] for s in visit["staff"]}
    assert per_staff == {salon["a"]: 800, salon["b"]: 2500}
    assert set(visit["chargeableBookingIds"]) == {b["bookingId"] for b in visit["bookings"]}

    # The original booking now points at the visit too.
    original = (await client.get(f"/api/v1/bookings/{hair['bookingId']}", headers=auth_headers)).json()
    assert original["visitId"] == visit["visitId"]
    assert (await client.get(f"/api/v1/visits/{visit['visitId']}", headers=auth_headers)).json()["total"]["amount"] == 3300


@pytest.mark.asyncio
async def test_add_after_the_same_person(client, auth_headers, salon):
    hair = await _walk_in(client, auth_headers, salon)
    resp = await _add(client, auth_headers, hair["bookingId"], salon, staff="a", start="after")
    assert resp.status_code == 201, resp.text
    visit = resp.json()
    added = next(b for b in visit["bookings"] if b["bookingId"] != hair["bookingId"])
    assert added["status"] == "confirmed"
    assert added["startsAt"] >= hair["startsAt"]
    assert [n["bookingId"] for n in visit["notReady"]] == [added["bookingId"]]
    assert visit["chargeableBookingIds"] == [hair["bookingId"]]


@pytest.mark.asyncio
async def test_busy_person_gets_next_free_time(client, auth_headers, salon):
    hair = await _walk_in(client, auth_headers, salon)
    resp = await _add(client, auth_headers, hair["bookingId"], salon, staff="a", start="now")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "slot_unavailable"
    assert "nextFreeAt" in body


@pytest.mark.asyncio
async def test_more_services_join_the_same_visit(client, auth_headers, salon, db_session):
    salon["c"] = str(uuid.uuid4())
    db_session.add(Staff(id=uuid.UUID(salon["c"]), business_id=uuid.UUID(salon["id"]), name="Achieng", role="staff"))
    await db_session.commit()
    hair = await _walk_in(client, auth_headers, salon)
    first = (await _add(client, auth_headers, hair["bookingId"], salon, staff="b", start="now")).json()
    second = await _add(client, auth_headers, hair["bookingId"], salon, staff="c", service="cut", start="now")
    assert second.status_code == 201, second.text
    assert second.json()["visitId"] == first["visitId"]
    assert len(second.json()["bookings"]) == 3


@pytest.mark.asyncio
async def test_no_confirmation_message_for_added_services(client, auth_headers, salon, monkeypatch):
    sent = []

    async def fake_notify(booking_id):
        sent.append(booking_id)

    monkeypatch.setattr(booking_tasks, "notify_confirmed", fake_notify)
    hair = await _walk_in(client, auth_headers, salon)
    assert (await _add(client, auth_headers, hair["bookingId"], salon, staff="a", start="after")).status_code == 201
    assert sent == []  # they're in the chair; no "your booking is confirmed" text


@pytest.mark.asyncio
async def test_cancelling_one_part_updates_the_visit(client, auth_headers, salon):
    hair = await _walk_in(client, auth_headers, salon)
    visit = (await _add(client, auth_headers, hair["bookingId"], salon, staff="a", start="after")).json()
    added = next(b for b in visit["bookings"] if b["bookingId"] != hair["bookingId"])
    assert (await client.patch(f"/api/v1/bookings/{added['bookingId']}/status", json={"status": "cancelled"}, headers=auth_headers)).status_code == 200

    after = (await client.get(f"/api/v1/visits/{visit['visitId']}", headers=auth_headers)).json()
    assert after["total"]["amount"] == 800
    assert after["notReady"] == []
    assert len(after["bookings"]) == 2  # still listed, just not counted


@pytest.mark.asyncio
async def test_validation_and_permissions(client, auth_headers, salon, db_session):
    hair = await _walk_in(client, auth_headers, salon)
    assert (await _add(client, auth_headers, hair["bookingId"], salon, start="at")).status_code == 400  # needs a time

    stylist = await make_user(db_session, "stylist.visit@example.com")
    db_session.add(Staff(business_id=uuid.UUID(salon["id"]), user_id=stylist.id, name="Imani", role="staff"))
    await db_session.commit()
    assert (await _add(client, headers_for(stylist), hair["bookingId"], salon)).status_code == 403

    visit = (await _add(client, auth_headers, hair["bookingId"], salon)).json()
    assert (await client.get(f"/api/v1/visits/{visit['visitId']}", headers=headers_for(stylist))).status_code == 403

    cancelled = (await client.post(
        "/api/v1/bookings/scheduled", json=owner_body(salon, staff="b", date=day().isoformat(), time="11:00"), headers=auth_headers,
    )).json()
    assert (await client.patch(f"/api/v1/bookings/{cancelled['bookingId']}/status", json={"status": "cancelled"}, headers=auth_headers)).status_code == 200
    resp = await _add(client, auth_headers, cancelled["bookingId"], salon, staff="a", service="cut")
    assert resp.status_code == 409 and resp.json()["error"] == "invalid_source_status"


@pytest.mark.asyncio
async def test_single_bookings_have_no_visit(client, auth_headers, salon):
    hair = await _walk_in(client, auth_headers, salon)
    assert hair["visitId"] is None
