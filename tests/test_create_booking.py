import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.security import create_access_token
from app.modules.auth.models import AuthType, User
from app.modules.booking.models import Booking
from app.modules.booking.states import record_created
from app.modules.business.models import Business, Staff

NBO = ZoneInfo("Africa/Nairobi")
ALL_WEEK = {d: {"open": "09:00", "close": "17:00"} for d in
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}


def day():
    return (datetime.now(NBO) + timedelta(days=7)).date()


@pytest.fixture
async def customer(db_session):
    user = User(id=uuid.uuid4(), email="amina@example.com", phone="+254722000111", first_name="Amina",
                last_name="W", auth_type=AuthType.EMAIL, verified=True)
    db_session.add(user)
    await db_session.commit()
    return user, {"Authorization": f"Bearer {create_access_token(data={'sub': str(user.id), 'email': user.email})}"}


@pytest.fixture
async def salon(client, auth_headers, db_session):
    biz = await client.post(
        "/api/v1/businesses",
        json={"name": "Glow Beauty", "categories": ["hair_salon"], "phone": "+254700331122",
              "location": {"address": "Kilimani", "lat": -1.29, "lng": 36.78}},
        headers=auth_headers,
    )
    biz_id = biz.json()["businessId"]
    await client.put(f"/api/v1/businesses/{biz_id}/working-hours", json={"weeklyHours": ALL_WEEK}, headers=auth_headers)
    cat = (await client.post(f"/api/v1/businesses/{biz_id}/service-categories", json={"name": "Hair"}, headers=auth_headers)).json()
    services = {}
    for name, minutes, price in (("cut", 45, 800), ("colour", 90, 2500)):
        svc = await client.post(
            f"/api/v1/businesses/{biz_id}/services",
            json={"categoryId": cat["categoryId"], "name": name, "durationMinutes": minutes, "price": {"amount": price, "currency": "KES"}},
            headers=auth_headers,
        )
        services[name] = svc.json()["serviceId"]
    a = Staff(id=uuid.uuid4(), business_id=uuid.UUID(biz_id), name="Wanjiku", role="staff")
    b = Staff(id=uuid.uuid4(), business_id=uuid.UUID(biz_id), name="Otieno", role="staff")
    db_session.add_all([a, b])
    business = await db_session.get(Business, uuid.UUID(biz_id))
    business.status = "published"
    await db_session.commit()
    return {"id": biz_id, "services": services, "a": str(a.id), "b": str(b.id)}


def body(salon, time="10:00", staff="a", services=(("cut", 1),), **extra):
    return {
        "businessId": salon["id"],
        "staffId": salon[staff] if staff else None,
        "services": [{"serviceId": salon["services"][name], "quantity": qty} for name, qty in services],
        "date": day().isoformat(),
        "time": time,
        **extra,
    }


async def book(client, headers, payload):
    return await client.post("/api/v1/bookings", json=payload, headers=headers)


async def set_deposit(client, auth_headers, salon, deposit):
    resp = await client.put(
        f"/api/v1/businesses/{salon['id']}/policies",
        json={"deposit": deposit, "cancellation": {"freeCancellationHours": 24, "lateFeePercent": 50}, "noShow": {"feePercent": 100}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text


# ─── Happy paths ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_deposit_confirms_immediately(client, salon, customer):
    _, headers = customer
    resp = await book(client, headers, body(salon, notes="Short on the sides"))
    assert resp.status_code == 201, resp.text
    b = resp.json()
    assert b["status"] == "confirmed"
    assert b["reference"].startswith("BK")
    assert (b["businessName"], b["customerName"], b["customerPhone"]) == ("Glow Beauty", "Amina W", "+254722000111")
    assert (b["staffId"], b["staffName"]) == (salon["a"], "Wanjiku")
    assert b["services"] == [{"serviceId": salon["services"]["cut"], "name": "cut", "durationMinutes": 45,
                              "price": {"amount": 800, "currency": "KES"}, "quantity": 1}]
    assert (b["date"], b["time"], b["durationMinutes"]) == (day().isoformat(), "10:00", 45)
    assert b["startsAt"] == f"{day().isoformat()}T10:00:00+03:00"
    assert b["totalAmount"] == {"amount": 800, "currency": "KES"}
    assert (b["depositAmount"], b["expiresAt"], b["paymentStatus"], b["source"]) == (None, None, "unpaid", "online")
    assert b["notes"] == "Short on the sides"


@pytest.mark.asyncio
async def test_booked_slot_leaves_availability(client, salon, customer):
    _, headers = customer
    await book(client, headers, body(salon))
    resp = await client.get(
        f"/api/v1/businesses/{salon['id']}/availability",
        params={"date": day().isoformat(), "serviceIds": salon["services"]["cut"], "staffId": salon["a"]},
    )
    times = [s["time"] for s in resp.json()["slots"]]
    assert "10:00" not in times and "09:30" not in times
    assert "09:15" in times and "10:45" in times


@pytest.mark.asyncio
async def test_deposit_holds_the_slot(client, auth_headers, salon, customer):
    _, headers = customer
    await set_deposit(client, auth_headers, salon, {"required": True, "type": "percent", "percent": 30, "appliesTo": "all_services"})
    before = datetime.now(timezone.utc)
    resp = await book(client, headers, body(salon, services=(("cut", 1), ("colour", 1))))
    assert resp.status_code == 201, resp.text
    b = resp.json()
    assert b["status"] == "pending_payment"
    assert b["totalAmount"] == {"amount": 3300, "currency": "KES"}
    assert b["depositAmount"] == {"amount": 990, "currency": "KES"}
    assert b["durationMinutes"] == 135
    expires = datetime.fromisoformat(b["expiresAt"])
    assert before + timedelta(minutes=9) < expires < before + timedelta(minutes=11)


@pytest.mark.asyncio
async def test_quantity_multiplies_price_and_duration(client, salon, customer):
    _, headers = customer
    b = (await book(client, headers, body(salon, services=(("cut", 2),)))).json()
    assert (b["totalAmount"]["amount"], b["durationMinutes"], b["services"][0]["quantity"]) == (1600, 90, 2)


@pytest.mark.asyncio
async def test_any_available_is_unassigned_and_uses_capacity(client, salon, customer):
    _, headers = customer
    first = await book(client, headers, body(salon, staff=None))
    assert first.status_code == 201, first.text
    assert (first.json()["staffId"], first.json()["staffName"]) == (None, "Any available")
    assert (await book(client, headers, body(salon, staff=None))).status_code == 201  # second staff member
    third = await book(client, headers, body(salon, staff=None))
    assert third.status_code == 409 and third.json()["error"] == "slot_unavailable"
    # A specific request is blocked too: both people are needed for the unassigned pair.
    assert (await book(client, headers, body(salon, staff="a"))).status_code == 409


# ─── Conflicts and holds ─────────────────────────────────────


@pytest.mark.asyncio
async def test_same_staff_same_time_is_409(client, salon, customer):
    _, headers = customer
    assert (await book(client, headers, body(salon))).status_code == 201
    clash = await book(client, headers, body(salon, time="10:30"))
    assert clash.status_code == 409 and clash.json()["error"] == "slot_unavailable"
    assert (await book(client, headers, body(salon, time="10:30", staff="b"))).status_code == 201


@pytest.mark.asyncio
async def test_expired_hold_is_released_for_the_new_booking(client, salon, customer, db_session):
    user, headers = customer
    start = datetime.combine(day(), datetime.min.time(), NBO).replace(hour=10).astimezone(timezone.utc)
    stale = Booking(
        business_id=uuid.UUID(salon["id"]), staff_id=uuid.UUID(salon["a"]), customer_id=user.id,
        starts_at=start, ends_at=start + timedelta(minutes=45), local_date=day(), local_time="10:00",
        duration_minutes=45, total_amount=800, customer_name="Someone", deposit_amount=200,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    record_created(stale, "pending_payment")
    db_session.add(stale)
    await db_session.commit()
    stale_id = stale.id

    assert (await book(client, headers, body(salon))).status_code == 201
    db_session.expire_all()
    reloaded = (await db_session.execute(
        select(Booking).where(Booking.id == stale_id).options(selectinload(Booking.events))
    )).scalar_one()
    assert (reloaded.status, reloaded.cancel_reason) == ("cancelled", "payment_expired")
    assert [e.to_status for e in reloaded.events] == ["pending_payment", "cancelled"]


# ─── Validation ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("time", ["10:05", "08:00", "16:30"])  # off the grid, before opening, would end after close
async def test_times_that_are_not_slots(client, salon, customer, time):
    _, headers = customer
    resp = await book(client, headers, body(salon, time=time))
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_price_changed(client, salon, customer):
    _, headers = customer
    resp = await book(client, headers, body(salon, expectedTotal={"amount": 700, "currency": "KES"}))
    assert resp.status_code == 409
    assert resp.json()["error"] == "price_changed"
    assert resp.json()["totalAmount"] == {"amount": 800, "currency": "KES"}
    assert (await book(client, headers, body(salon, expectedTotal={"amount": 800, "currency": "KES"}))).status_code == 201


@pytest.mark.asyncio
async def test_owner_cannot_book_own_business(client, auth_headers, salon):
    resp = await book(client, auth_headers, body(salon))
    assert resp.status_code == 400 and resp.json()["error"] == "own_business"


@pytest.mark.asyncio
async def test_duplicate_service_lines(client, salon, customer):
    _, headers = customer
    resp = await book(client, headers, body(salon, services=(("cut", 1), ("cut", 1))))
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_staff_who_cannot_perform(client, salon, customer, db_session):
    _, headers = customer
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.performs_all_services = False
    await db_session.commit()
    resp = await book(client, headers, body(salon))
    assert resp.status_code == 400 and resp.json()["error"] == "staff_cannot_perform"


@pytest.mark.asyncio
async def test_unpublished_business(client, salon, customer, db_session):
    _, headers = customer
    business = await db_session.get(Business, uuid.UUID(salon["id"]))
    business.status = "draft"
    await db_session.commit()
    assert (await book(client, headers, body(salon))).status_code == 404


@pytest.mark.asyncio
async def test_requires_sign_in(client, salon):
    assert (await client.post("/api/v1/bookings", json=body(salon))).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [{"time": "25:00"}, {"services": []}, {"date": "not-a-date"}])
async def test_request_validation(client, salon, customer, patch):
    _, headers = customer
    assert (await book(client, headers, {**body(salon), **patch})).status_code == 422
