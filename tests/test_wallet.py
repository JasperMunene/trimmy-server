import uuid
from datetime import datetime, timedelta, timezone

import pytest

import app.modules.notifications.tasks as notification_tasks
from app.modules.booking.models import Booking
from app.modules.business.models import Staff
from app.modules.payouts.wallet import Movement, running_balances
from tests.test_create_booking import customer, salon  # noqa: F401  (fixtures)
from tests.test_payouts import balance, paid_visit, request, stylist  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, insert_booking, make_user

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
BIZ = uuid.uuid4()


def move(minutes, type_, amount, key):
    return Movement(at=T0 + timedelta(minutes=minutes), type=type_, amount=amount, key=key, business_id=BIZ, description="")


def test_running_balance_oldest_first_and_money_in_before_out_on_ties():
    ledger = running_balances([
        move(10, "payout", -300, "p1"), move(0, "commission", 320, "c1"), move(10, "tip", 100, "t1"),
        move(20, "deduction", -50, "d1"), move(30, "payout", 0, "p2"),  # a rejected payout
    ])
    assert [(m.key, after) for m, after in ledger] == [("c1", 320), ("t1", 420), ("p1", 120), ("d1", 70), ("p2", 70)]


async def adjust(client, headers, salon, kind, amount, staff="a", **extra):
    return await client.post(f"/api/v1/businesses/{salon['id']}/staff/{salon[staff]}/wallet-adjustments",
                             json={"kind": kind, "amount": amount, **extra}, headers=headers)


@pytest.fixture
def pushes(monkeypatch):
    calls = []

    async def record(user_id, title, body, **data):
        calls.append((title, body, data.get("type")))

    monkeypatch.setattr(notification_tasks, "notify", record)
    return calls


@pytest.mark.asyncio
async def test_adjustments_move_the_balance(client, auth_headers, salon, stylist, pushes):
    booking_id = await paid_visit(client, auth_headers, salon)  # +320 commission
    tip = await adjust(client, auth_headers, salon, "tip", 200, note="From Amina", bookingId=booking_id)
    assert tip.status_code == 201, tip.text
    assert (tip.json()["kind"], tip.json()["signedAmount"], tip.json()["bookingId"]) == ("tip", 200, booking_id)
    await adjust(client, auth_headers, salon, "bonus", 500)
    await adjust(client, auth_headers, salon, "deduction", 100, note="Broken mirror")
    await adjust(client, auth_headers, salon, "advance", 300)

    b = await balance(client, stylist, salon)
    assert (b["earned"], b["tips"], b["bonuses"], b["deductions"], b["advances"]) == (320, 200, 500, 100, 300)
    assert b["available"] == 320 + 200 + 500 - 100 - 300
    assert pushes[0] == ("Tip recorded", "+KES 200 in your wallet: From Amina", "wallet_adjusted")
    assert pushes[2][1] == "-KES 100 in your wallet: Broken mirror"

    # Payouts are checked against the new balance.
    assert (await request(client, stylist, salon, 621)).status_code == 409
    assert (await request(client, stylist, salon, 620)).status_code == 201


@pytest.mark.asyncio
async def test_void_takes_it_back_out(client, auth_headers, salon, stylist):
    await paid_visit(client, auth_headers, salon)
    bonus = (await adjust(client, auth_headers, salon, "bonus", 500)).json()
    voided = await client.post(f"/api/v1/wallet-adjustments/{bonus['adjustmentId']}/void", headers=auth_headers)
    assert voided.status_code == 200 and voided.json()["voidedAt"] is not None
    assert (await balance(client, stylist, salon))["available"] == 320
    again = await client.post(f"/api/v1/wallet-adjustments/{bonus['adjustmentId']}/void", headers=auth_headers)
    assert again.status_code == 409 and again.json()["error"] == "already_voided"
    # Staff can't void their own deductions.
    deduction = (await adjust(client, auth_headers, salon, "deduction", 50)).json()
    assert (await client.post(f"/api/v1/wallet-adjustments/{deduction['adjustmentId']}/void", headers=stylist)).status_code == 404


@pytest.mark.asyncio
async def test_who_can_adjust(client, auth_headers, salon, stylist, db_session):
    desk_user = await make_user(db_session, "desk.wallet@example.com")
    db_session.add(Staff(id=uuid.uuid4(), business_id=uuid.UUID(salon["id"]), user_id=desk_user.id, name="Faith",
                         role="front_desk", performs_all_services=False))
    await db_session.commit()
    desk = headers_for(desk_user)
    assert (await adjust(client, desk, salon, "tip", 100)).status_code == 201
    assert (await adjust(client, desk, salon, "bonus", 100)).status_code == 403
    assert (await adjust(client, stylist, salon, "bonus", 100)).status_code == 403
    assert (await adjust(client, auth_headers, salon, "tip", 0)).status_code == 422
    assert (await adjust(client, auth_headers, salon, "refund", 10)).status_code == 422
    other = await adjust(client, auth_headers, salon, "tip", 10, bookingId=str(uuid.uuid4()))
    assert other.status_code == 404


@pytest.mark.asyncio
async def test_statement_newest_first_with_running_balance(client, auth_headers, salon, stylist):
    booking_id = await paid_visit(client, auth_headers, salon)  # +320
    await adjust(client, auth_headers, salon, "tip", 200)
    payout = (await request(client, stylist, salon, 400)).json()
    await client.post(f"/api/v1/payouts/{payout['payoutId']}/respond", json={"action": "reject", "rejectionReason": "Friday"},
                      headers=auth_headers)
    await request(client, stylist, salon, 100)

    resp = await client.get("/api/v1/me/wallet/transactions", params={"businessId": salon["id"]}, headers=stylist)
    assert resp.status_code == 200, resp.text
    s = resp.json()
    assert [(e["type"], e["amount"], e["balanceAfter"]) for e in s["entries"]] == [
        ("payout", -100, 420), ("payout", 0, 520), ("tip", 200, 520), ("commission", 320, 320),
    ]
    assert (s["balance"], s["total"], s["nextOffset"]) == (420, 4, None)
    assert s["balance"] == (await balance(client, stylist, salon))["available"]
    rejected, commission = s["entries"][1], s["entries"][3]
    assert (rejected["payoutStatus"], rejected["payoutAmount"], rejected["note"]) == ("rejected", 400, "Friday")
    assert commission["bookingId"] == booking_id and commission["bookingReference"]
    assert commission["businessName"] == "Glow Beauty"

    page = (await client.get("/api/v1/me/wallet/transactions", params={"limit": 3}, headers=stylist)).json()
    assert len(page["entries"]) == 3 and page["nextOffset"] == 3
    rest = (await client.get("/api/v1/me/wallet/transactions", params={"limit": 3, "offset": 3}, headers=stylist)).json()
    assert [e["type"] for e in rest["entries"]] == ["commission"] and rest["nextOffset"] is None

    owner_view = await client.get(f"/api/v1/businesses/{salon['id']}/staff/{salon['a']}/wallet/transactions", headers=auth_headers)
    assert owner_view.json()["entries"] == s["entries"]
    assert (await client.get(f"/api/v1/businesses/{salon['id']}/staff/{salon['b']}/wallet/transactions", headers=stylist)).status_code == 403


@pytest.mark.asyncio
async def test_one_wallet_across_businesses(client, auth_headers, salon, stylist, db_session):
    await paid_visit(client, auth_headers, salon)  # 320 at Glow Beauty
    # The same person also works at a second salon, where they earned 150.
    me = (await db_session.get(Staff, uuid.UUID(salon["a"]))).user_id
    other_owner = headers_for(await make_user(db_session, "owner.two@example.com"))
    second = (await client.post("/api/v1/businesses", json={
        "name": "Fade Studio", "categories": ["barbershop"], "phone": "+254700889900",
        "location": {"address": "CBD", "lat": -1.28, "lng": 36.82}}, headers=other_owner)).json()["businessId"]
    there = Staff(id=uuid.uuid4(), business_id=uuid.UUID(second), user_id=me, name="Wanjiku", role="staff", commission_percent=30)
    db_session.add(there)
    await db_session.commit()
    visit = await insert_booking(db_session, {"id": second, "a": str(there.id)}, me, datetime.now(timezone.utc), status="completed")
    visit.payment_status, visit.staff_commission, visit.paid_at = "paid", 150, datetime.now(timezone.utc)
    await db_session.commit()

    w = (await client.get("/api/v1/me/wallet", headers=stylist)).json()
    assert (w["earned"], w["available"]) == (470, 470)
    assert {(b["businessName"], b["available"], b["role"]) for b in w["businesses"]} == {("Glow Beauty", 320, "staff"), ("Fade Studio", 150, "staff")}
    everything = (await client.get("/api/v1/me/wallet/transactions", headers=stylist)).json()
    assert everything["balance"] == 470
    assert {e["businessName"] for e in everything["entries"]} == {"Glow Beauty", "Fade Studio"}
    assert (await client.get("/api/v1/me/wallet/transactions", params={"businessId": str(uuid.uuid4())}, headers=stylist)).status_code == 404
    # Nobody else's wallet leaks in.
    assert (await client.get("/api/v1/me/wallet", headers=other_owner)).json()["businesses"] == []


@pytest.mark.asyncio
async def test_paid_at_is_set_when_charged(client, auth_headers, salon, stylist, db_session):
    booking_id = await paid_visit(client, auth_headers, salon)
    db_session.expire_all()
    assert (await db_session.get(Booking, uuid.UUID(booking_id))).paid_at is not None
