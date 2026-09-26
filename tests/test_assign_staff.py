import uuid

import pytest
from sqlalchemy import select

from app.modules.booking.models import BookingEvent
from app.modules.business.models import Staff
from tests.test_create_booking import body, book, customer, day, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


async def unassigned(client, cust_headers, salon, time="10:00"):
    resp = await book(client, cust_headers, body(salon, staff=None, time=time))
    assert resp.status_code == 201, resp.text
    return resp.json()["bookingId"]


async def assign(client, headers, booking_id, staff_id):
    return await client.patch(f"/api/v1/bookings/{booking_id}/staff", json={"staffId": staff_id}, headers=headers)


@pytest.mark.asyncio
async def test_owner_assigns_and_reassigns(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    booking_id = await unassigned(client, cust, salon)

    resp = await assign(client, auth_headers, booking_id, salon["a"])
    assert resp.status_code == 200, resp.text
    assert (resp.json()["staffId"], resp.json()["staffName"]) == (salon["a"], "Wanjiku")
    # The customer sees it too.
    seen = (await client.get(f"/api/v1/bookings/{booking_id}", headers=cust)).json()
    assert seen["staffName"] == "Wanjiku"

    resp = await assign(client, auth_headers, booking_id, salon["b"])
    assert resp.json()["staffId"] == salon["b"]

    reasons = (await db_session.execute(
        select(BookingEvent.reason).where(BookingEvent.booking_id == uuid.UUID(booking_id)).order_by(BookingEvent.at)
    )).scalars().all()
    assert reasons[-2:] == ["staff_assigned", "staff_reassigned"]


@pytest.mark.asyncio
async def test_cannot_assign_someone_already_busy(client, auth_headers, salon, customer):
    _, cust = customer
    assert (await book(client, cust, body(salon, staff="a", time="10:15"))).status_code == 201
    booking_id = await unassigned(client, cust, salon, time="10:00")
    resp = await assign(client, auth_headers, booking_id, salon["a"])
    assert resp.status_code == 409 and resp.json()["error"] == "staff_unavailable"
    assert (await assign(client, auth_headers, booking_id, salon["b"])).status_code == 200


@pytest.mark.asyncio
async def test_capacity_with_two_unassigned(client, auth_headers, salon, customer):
    _, cust = customer
    first, second = await unassigned(client, cust, salon), await unassigned(client, cust, salon)
    assert (await assign(client, auth_headers, first, salon["a"])).status_code == 200
    assert (await assign(client, auth_headers, second, salon["a"])).status_code == 409
    assert (await assign(client, auth_headers, second, salon["b"])).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["no_services", "day_off"])
async def test_staff_must_be_eligible(client, auth_headers, salon, customer, db_session, change):
    _, cust = customer
    booking_id = await unassigned(client, cust, salon)
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    if change == "no_services":
        member.performs_all_services = False
    else:
        other_days = [d for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
                      if d != day().strftime("%A").lower()]
        member.working_days = other_days
    await db_session.commit()
    resp = await assign(client, auth_headers, booking_id, salon["a"])
    assert resp.status_code == 409 and resp.json()["error"] == "staff_unavailable"


@pytest.mark.asyncio
async def test_who_may_assign(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    booking_id = await unassigned(client, cust, salon)

    assert (await assign(client, cust, booking_id, salon["a"])).status_code == 403  # the customer

    stylist = await make_user(db_session, "stylist@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["b"]))
    member.user_id = stylist.id
    await db_session.commit()
    assert (await assign(client, headers_for(stylist), booking_id, salon["a"])).status_code == 403  # regular staff

    desk = await make_user(db_session, "desk@example.com")
    db_session.add(Staff(id=uuid.uuid4(), business_id=uuid.UUID(salon["id"]), user_id=desk.id, name="Desk",
                         role="front_desk", performs_all_services=False))
    await db_session.commit()
    assert (await assign(client, headers_for(desk), booking_id, salon["a"])).status_code == 200  # front desk

    stranger = await make_user(db_session, "stranger2@example.com")
    assert (await assign(client, headers_for(stranger), booking_id, salon["a"])).status_code == 404


@pytest.mark.asyncio
async def test_inactive_booking_and_foreign_staff(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    booking_id = await unassigned(client, cust, salon)
    assert (await assign(client, auth_headers, booking_id, str(uuid.uuid4()))).status_code == 404

    from app.modules.booking.models import Booking
    from app.modules.booking.states import transition
    b = await db_session.get(Booking, uuid.UUID(booking_id))
    transition(b, "cancelled", reason="customer")
    await db_session.commit()
    resp = await assign(client, auth_headers, booking_id, salon["a"])
    assert resp.status_code == 409 and resp.json()["error"] == "booking_not_active"
