"""Front desk: not bookable by default, listed on the profile, and able to use every
"owner or front desk" endpoint (fulfillment.json#frontDeskNote)."""
import uuid
from datetime import datetime, timedelta

import pytest

from app.modules.business.models import Business, Staff
from tests.test_business_bookings import owner_body
from tests.test_create_booking import NBO, body, book, customer, day, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user
from tests.test_staff_identity import MEMBER_PHONE, _business, _invite_and_accept, member  # noqa: F401


@pytest.mark.asyncio
async def test_accepted_front_desk_is_not_bookable(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id = await _business(client, auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id, role="front_desk")
    (roster,) = (await client.get(f"/api/v1/businesses/{biz_id}/team/members", headers=auth_headers)).json()["members"]
    staff = await db_session.get(Staff, uuid.UUID(roster["staffId"]))
    assert (staff.role, staff.performs_all_services) == ("front_desk", False)

    business = await db_session.get(Business, uuid.UUID(biz_id))
    business.status = "published"
    await db_session.commit()
    listed = [s for s in (await client.get(f"/api/v1/businesses/{biz_id}/profile")).json()["staff"] if s["staffId"] == roster["staffId"]]
    assert listed == []  # customers never see the front desk
    cust = headers_for(await make_user(db_session, "fav.desk@example.com"))
    assert (await client.put(f"/api/v1/me/favorites/staff/{roster['staffId']}", headers=cust)).status_code == 404


@pytest.mark.asyncio
async def test_profile_lists_every_accepted_stylist(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id = await _business(client, auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id)  # no rating, no photo
    business = await db_session.get(Business, uuid.UUID(biz_id))
    business.status = "published"
    await db_session.commit()
    (roster,) = (await client.get(f"/api/v1/businesses/{biz_id}/team/members", headers=auth_headers)).json()["members"]
    staff_ids = [s["staffId"] for s in (await client.get(f"/api/v1/businesses/{biz_id}/profile")).json()["staff"]]
    assert roster["staffId"] in staff_ids


@pytest.mark.asyncio
async def test_front_desk_can_use_every_owner_or_front_desk_endpoint(client, auth_headers, salon, customer, db_session):
    _, cust = customer
    user = await make_user(db_session, "desk.all@example.com")
    db_session.add(Staff(id=uuid.uuid4(), business_id=uuid.UUID(salon["id"]), user_id=user.id, name="Faith",
                         role="front_desk", performs_all_services=False))
    await db_session.commit()
    desk = headers_for(user)
    bid = salon["id"]

    assert (await client.get(f"/api/v1/businesses/{bid}/bookings", params={"date": day().isoformat()}, headers=desk)).status_code == 200
    walk_in = await client.post("/api/v1/bookings/walk-in", json=owner_body(salon, staff="a"), headers=desk)
    assert walk_in.status_code == 201, walk_in.text
    scheduled = await client.post("/api/v1/bookings/scheduled", json=owner_body(salon, customerPhone="0733000111", date=day().isoformat(), time="15:00"), headers=desk)
    assert scheduled.status_code == 201, scheduled.text
    assert (await client.patch(f"/api/v1/bookings/{scheduled.json()['bookingId']}/staff", json={"staffId": salon["b"]}, headers=desk)).status_code == 200
    assert (await client.post(f"/api/v1/bookings/{walk_in.json()['bookingId']}/charge", json={"method": "cash"}, headers=desk)).status_code == 200
    online = (await book(client, cust, body(salon, time="10:00"))).json()["bookingId"]
    assert (await client.patch(f"/api/v1/bookings/{online}/status", json={"status": "cancelled"}, headers=desk)).status_code == 200
    moved = await client.post(f"/api/v1/bookings/{scheduled.json()['bookingId']}/reschedule", json={"date": day().isoformat(), "time": "16:00"}, headers=desk)
    assert moved.status_code == 200, moved.text
    assert (await client.get(f"/api/v1/businesses/{bid}/customers", headers=desk)).status_code == 200
    assert (await client.post(f"/api/v1/businesses/{bid}/customers", json={"name": "Zawadi", "phone": "0700999888"}, headers=desk)).status_code == 200
    assert (await client.get(f"/api/v1/businesses/{bid}/bookings", params={"refund": "due"}, headers=desk)).status_code == 200
    assert (await client.get(f"/api/v1/businesses/{bid}/day-summary", params={"date": day().isoformat()}, headers=desk)).status_code == 200
    # Owner-only stays owner-only.
    assert (await client.get(f"/api/v1/businesses/{bid}/earnings", params={"from": day().isoformat(), "to": day().isoformat()}, headers=desk)).status_code == 403
