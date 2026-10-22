"""Front desk roles: desk-only members earn no commission; staff can also have front desk access
(one person, two modes), set by the owner."""
import uuid

import pytest
from sqlalchemy import select

from app.modules.booking.models import Booking
from app.modules.business.models import Staff
from tests.test_business_bookings import owner_body
from tests.test_create_booking import ALL_WEEK, body, book, customer, day, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user
from tests.test_staff_identity import MEMBER_PHONE, _business, _invite_and_accept, member  # noqa: F401


def _members(biz_id):
    return f"/api/v1/businesses/{biz_id}/team/members"


async def _roster(client, headers, biz_id):
    (m,) = (await client.get(_members(biz_id), headers=headers)).json()["members"]
    return m


# ─── Desk-only: no commission ────────────────────────────────


@pytest.mark.asyncio
async def test_front_desk_invite_has_no_commission(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    added = await client.post(
        _members(biz_id), json={"name": "Faith", "phone": "+254712000111", "role": "front_desk", "commissionPercent": 40},
        headers=auth_headers,
    )
    assert added.status_code == 201, added.text
    assert added.json()["commissionPercent"] is None  # a percent sent by older apps is ignored
    assert added.json()["frontDeskAccess"] is True

    staff = await client.post(_members(biz_id), json={"name": "Akinyi", "phone": "+254712000112", "role": "staff"}, headers=auth_headers)
    assert (staff.json()["commissionPercent"], staff.json()["frontDeskAccess"]) == (40, False)


@pytest.mark.asyncio
async def test_accepted_front_desk_staff_row_has_no_commission(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id = await _business(client, auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id, role="front_desk")
    roster = await _roster(client, auth_headers, biz_id)
    staff = await db_session.get(Staff, uuid.UUID(roster["staffId"]))
    assert (staff.role, staff.commission_percent, staff.front_desk_access) == ("front_desk", None, True)

    # Editing their commission is ignored too.
    patched = await client.patch(f"{_members(biz_id)}/{roster['invitationId']}", json={"commissionPercent": 30}, headers=auth_headers)
    assert patched.status_code == 200 and patched.json()["commissionPercent"] is None


@pytest.mark.asyncio
async def test_charging_never_pays_commission_to_desk_only(client, auth_headers, salon, db_session):
    desk = Staff(id=uuid.uuid4(), business_id=uuid.UUID(salon["id"]), name="Desk", role="front_desk",
                 commission_percent=40, front_desk_access=True)  # a stale percent from before the change
    db_session.add(desk)
    await db_session.commit()
    walk_in = (await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="a"), headers=auth_headers)).json()
    booking_id = uuid.UUID(walk_in["bookingId"])
    booking = await db_session.get(Booking, booking_id)
    booking.staff_id = desk.id
    await db_session.commit()
    assert (await client.post(f"/api/v1/bookings/{walk_in['bookingId']}/charge", json={"method": "cash"}, headers=auth_headers)).status_code == 200
    db_session.expire_all()
    assert (await db_session.get(Booking, booking_id)).staff_commission in (None, 0)


# ─── Staff who also run the desk ─────────────────────────────


@pytest.mark.asyncio
async def test_staff_with_desk_access_can_run_the_desk_and_stay_bookable(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    user = await make_user(db_session, "dual@example.com")
    me = Staff(id=uuid.uuid4(), business_id=uuid.UUID(salon["id"]), user_id=user.id, name="Neema", role="staff",
               commission_percent=50, front_desk_access=True)
    db_session.add(me)
    await db_session.commit()
    dual = headers_for(user)
    bid = salon["id"]

    # Desk work: see everyone's day, take a walk-in for a colleague, charge it.
    assert (await client.get(f"/api/v1/businesses/{bid}/bookings", params={"date": day().isoformat()}, headers=dual)).status_code == 200
    walk_in = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="a"), headers=dual)
    assert walk_in.status_code == 201, walk_in.text
    assert (await client.post(f"/api/v1/bookings/{walk_in.json()['bookingId']}/charge", json={"method": "cash"}, headers=dual)).status_code == 200

    # Still bookable by customers, and commission only on their own work.
    salon["me"] = str(me.id)
    mine = await book(client, cust, body(salon, staff="me", time="15:00"))
    assert mine.status_code == 201, mine.text
    db_session.expire_all()
    colleague_booking = await db_session.get(Booking, uuid.UUID(walk_in.json()["bookingId"]))
    assert colleague_booking.staff_id == uuid.UUID(salon["a"])

    # Earnings: only their own, even with desk access.
    params = {"from": day().isoformat(), "to": day().isoformat()}
    own = await client.get(f"/api/v1/businesses/{bid}/earnings", params=params, headers=dual)
    assert own.status_code == 200 and own.json()["staffId"] == str(me.id)
    other = await client.get(f"/api/v1/businesses/{bid}/earnings", params={**params, "staffId": salon["a"]}, headers=dual)
    assert other.status_code == 403


@pytest.mark.asyncio
async def test_me_businesses_lists_modes(client, auth_headers, member):
    _, member_headers = member
    biz_id = await _business(client, auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id)
    roster = await _roster(client, auth_headers, biz_id)

    def mine(resp):
        return next(b for b in resp.json()["businesses"] if b["businessId"] == biz_id)

    plain = mine(await client.get("/api/v1/me/businesses", headers=member_headers))
    assert (plain["role"], plain["frontDeskAccess"], plain["modes"]) == ("staff", False, ["staff"])

    await client.patch(f"{_members(biz_id)}/{roster['invitationId']}", json={"frontDeskAccess": True}, headers=auth_headers)
    both = mine(await client.get("/api/v1/me/businesses", headers=member_headers))
    assert (both["role"], both["frontDeskAccess"], both["modes"]) == ("staff", True, ["staff", "front_desk"])


@pytest.mark.asyncio
async def test_owner_switches_what_a_member_does(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id = await _business(client, auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id)
    roster = await _roster(client, auth_headers, biz_id)
    url = f"{_members(biz_id)}/{roster['invitationId']}"

    both = (await client.patch(url, json={"frontDeskAccess": True}, headers=auth_headers)).json()
    assert (both["role"], both["frontDeskAccess"], both["commissionPercent"]) == ("staff", True, 45)

    desk_only = (await client.patch(url, json={"takesAppointments": False}, headers=auth_headers)).json()
    assert (desk_only["role"], desk_only["frontDeskAccess"], desk_only["commissionPercent"]) == ("front_desk", True, None)
    staff = await db_session.get(Staff, uuid.UUID(roster["staffId"]))
    await db_session.refresh(staff)
    assert (staff.role, staff.performs_all_services, staff.commission_percent) == ("front_desk", False, None)

    back = (await client.patch(url, json={"takesAppointments": True, "frontDeskAccess": False, "commissionPercent": 35}, headers=auth_headers)).json()
    assert (back["role"], back["frontDeskAccess"], back["commissionPercent"]) == ("staff", False, 35)
    await db_session.refresh(staff)
    assert staff.performs_all_services is True

    neither = await client.patch(url, json={"takesAppointments": False, "frontDeskAccess": False}, headers=auth_headers)
    assert neither.status_code == 400


@pytest.mark.asyncio
async def test_revoking_desk_access_takes_desk_actions_away(client, auth_headers, salon, db_session):
    user = await make_user(db_session, "revoke@example.com")
    me = Staff(id=uuid.uuid4(), business_id=uuid.UUID(salon["id"]), user_id=user.id, name="Neema", role="staff",
               commission_percent=50, front_desk_access=True)
    db_session.add(me)
    await db_session.commit()
    dual = headers_for(user)
    assert (await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="a"), headers=dual)).status_code == 201

    me.front_desk_access = False
    await db_session.commit()
    assert (await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="a"), headers=dual)).status_code == 403


@pytest.mark.asyncio
async def test_cannot_make_desk_only_while_they_have_appointments(client, auth_headers, member, customer, db_session):
    _, member_headers = member
    _, cust = customer
    biz_id = await _business(client, auth_headers)
    await client.put(f"/api/v1/businesses/{biz_id}/working-hours", json={"weeklyHours": ALL_WEEK}, headers=auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id, workingDays=None)
    roster = await _roster(client, auth_headers, biz_id)
    cat = (await client.post(f"/api/v1/businesses/{biz_id}/service-categories", json={"name": "Hair"}, headers=auth_headers)).json()
    svc = (await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={"categoryId": cat["categoryId"], "name": "Cut", "durationMinutes": 30, "price": {"amount": 500, "currency": "KES"}},
        headers=auth_headers,
    )).json()
    walk_in = await client.post(
        "/api/v1/bookings/walk-in",
        json={"businessId": biz_id, "services": [{"serviceId": svc["serviceId"], "quantity": 1}], "staffId": roster["staffId"]},
        headers=auth_headers,
    )
    assert walk_in.status_code == 201, walk_in.text

    resp = await client.patch(f"{_members(biz_id)}/{roster['invitationId']}", json={"takesAppointments": False, "frontDeskAccess": True}, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["error"] == "has_upcoming_bookings"
    assert (await db_session.execute(select(Staff.role).where(Staff.id == uuid.UUID(roster["staffId"])))).scalar_one() == "staff"


@pytest.mark.asyncio
async def test_only_the_owner_changes_roles(client, auth_headers, member):
    _, member_headers = member
    biz_id = await _business(client, auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id)
    roster = await _roster(client, auth_headers, biz_id)
    url = f"{_members(biz_id)}/{roster['invitationId']}"
    await client.patch(url, json={"frontDeskAccess": True}, headers=auth_headers)

    # Even with desk access, a member can't change what anyone does, themselves included.
    resp = await client.patch(url, json={"takesAppointments": False, "commissionPercent": 90}, headers=member_headers)
    assert resp.status_code == 404
    assert (await _roster(client, auth_headers, biz_id))["role"] == "staff"
