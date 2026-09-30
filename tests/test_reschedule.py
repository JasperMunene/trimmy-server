import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.modules.booking.tasks as booking_tasks
from app.modules.booking.models import Booking, BookingEvent
from tests.test_create_booking import body, book, customer, day, salon, set_deposit  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


@pytest.fixture
def notified(monkeypatch):
    calls = []

    async def record(booking_id):
        calls.append(str(booking_id))

    monkeypatch.setattr(booking_tasks, "notify_confirmed", record)
    return calls


async def move(client, headers, booking_id, time, date=None, **extra):
    return await client.post(f"/api/v1/bookings/{booking_id}/reschedule",
                             json={"date": (date or day()).isoformat(), "time": time, **extra}, headers=headers)


async def slots(client, salon, staff="a"):
    resp = await client.get(f"/api/v1/businesses/{salon['id']}/availability",
                            params={"date": day().isoformat(), "serviceIds": salon["services"]["cut"], "staffId": salon[staff]})
    return [s["time"] for s in resp.json()["slots"]]


@pytest.mark.asyncio
async def test_customer_moves_to_another_slot(client, salon, customer, notified, db_session):
    _, cust = customer
    booking = (await book(client, cust, body(salon, time="10:00"))).json()
    notified.clear()

    resp = await move(client, cust, booking["bookingId"], "14:15")
    assert resp.status_code == 200, resp.text
    b = resp.json()
    assert (b["time"], b["startsAt"], b["staffId"], b["reference"]) == (
        "14:15", f"{day().isoformat()}T14:15:00+03:00", salon["a"], booking["reference"])
    assert "10:00" in await slots(client, salon) and "14:15" not in await slots(client, salon)
    assert notified == [booking["bookingId"]]  # a fresh SMS for the new time
    reason = (await db_session.execute(
        select(BookingEvent.reason).where(BookingEvent.booking_id == uuid.UUID(booking["bookingId"])).order_by(BookingEvent.at.desc())
    )).scalars().first()
    assert reason == f"rescheduled from {day().isoformat()} 10:00"


@pytest.mark.asyncio
async def test_overlapping_its_own_old_slot_is_fine(client, salon, customer, notified):
    _, cust = customer
    booking = (await book(client, cust, body(salon, time="10:00"))).json()
    assert (await move(client, cust, booking["bookingId"], "10:30")).status_code == 200  # overlaps 10:00-10:45 itself


@pytest.mark.asyncio
async def test_move_to_another_staff_member(client, salon, customer, notified):
    _, cust = customer
    booking = (await book(client, cust, body(salon, time="10:00"))).json()
    b = (await move(client, cust, booking["bookingId"], "10:00", staffId=salon["b"])).json()
    assert (b["staffId"], b["staffName"]) == (salon["b"], "Otieno")


@pytest.mark.asyncio
async def test_taken_or_off_grid_slots(client, salon, customer, notified):
    _, cust = customer
    mine = (await book(client, cust, body(salon, time="10:00"))).json()
    await book(client, cust, body(salon, time="15:00"))
    assert (await move(client, cust, mine["bookingId"], "15:15")).json()["error"] == "slot_unavailable"
    assert (await move(client, cust, mine["bookingId"], "12:05")).status_code == 409  # customers stay on the grid


@pytest.mark.asyncio
async def test_deposit_and_payment_stay_with_the_booking(client, auth_headers, salon, customer, db_session, notified):
    _, cust = customer
    await set_deposit(client, auth_headers, salon, {"required": True, "type": "fixed", "amount": {"amount": 300, "currency": "KES"}, "appliesTo": "all_services"})
    booking = (await book(client, cust, body(salon, time="10:00"))).json()
    b = await db_session.get(Booking, uuid.UUID(booking["bookingId"]))
    b.payment_status = "deposit_paid"
    await db_session.commit()

    moved = (await move(client, cust, booking["bookingId"], "11:00")).json()
    assert (moved["status"], moved["paymentStatus"], moved["depositAmount"]["amount"]) == ("pending_payment", "deposit_paid", 300)
    assert moved["expiresAt"] == booking["expiresAt"]  # an unpaid hold keeps its deadline
    assert notified == []  # not confirmed yet: no SMS


@pytest.mark.asyncio
async def test_customer_window_and_rules(client, auth_headers, salon, customer, notified):
    _, cust = customer
    booking = (await book(client, cust, body(salon, time="10:00"))).json()
    # The business only allows free changes up to 30 days before: a week out is too late for the customer.
    await client.put(f"/api/v1/businesses/{salon['id']}/policies",
                     json={"deposit": {"required": False, "type": "fixed", "appliesTo": "all_services"},
                           "cancellation": {"freeCancellationHours": 24 * 30, "lateFeePercent": 50}, "noShow": {"feePercent": 100}},
                     headers=auth_headers)
    late = await move(client, cust, booking["bookingId"], "11:00")
    assert late.status_code == 409 and late.json()["error"] == "reschedule_window_passed"
    # The owner still can, and off the grid.
    owner = await move(client, auth_headers, booking["bookingId"], "11:05")
    assert owner.status_code == 200 and owner.json()["time"] == "11:05"


@pytest.mark.asyncio
async def test_business_rules(client, auth_headers, salon, customer, notified):
    _, cust = customer
    booking = (await book(client, cust, body(salon, time="10:00"))).json()
    assert (await move(client, auth_headers, booking["bookingId"], "08:00")).status_code == 400  # before opening
    past = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    assert (await move(client, auth_headers, booking["bookingId"], "10:00", date=past)).status_code == 400


@pytest.mark.asyncio
async def test_who_and_what_can_be_moved(client, auth_headers, salon, customer, db_session, notified):
    _, cust = customer
    booking = (await book(client, cust, body(salon, time="10:00"))).json()
    stranger = await make_user(db_session, "mover@example.com")
    assert (await move(client, headers_for(stranger), booking["bookingId"], "11:00")).status_code == 404

    await client.post(f"/api/v1/bookings/{booking['bookingId']}/cancel", json={}, headers=cust)
    gone = await move(client, cust, booking["bookingId"], "11:00")
    assert gone.status_code == 409 and gone.json()["error"] == "cannot_reschedule"
