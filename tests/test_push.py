import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.modules.notifications.push as push
import app.workers.registry as registry
from app.core.config import settings
from app.modules.business.models import Business, Staff
from app.modules.notifications.models import PushToken
from app.workers.registry import TASKS, discover_tasks
from tests.test_create_booking import customer, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, insert_booking, make_user

TOKEN = "ExponentPushToken[abc123abc123abc123abc1]"


async def register(client, headers, token=TOKEN, platform="android"):
    return await client.post("/api/v1/me/push-tokens", json={"token": token, "platform": platform}, headers=headers)


# ─── Registering ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_is_idempotent_and_moves_between_users(client, customer, db_session):
    user, h = customer
    assert (await register(client, h)).json() == {"ok": True}
    assert (await register(client, h, platform="ios")).status_code == 200
    rows = (await db_session.execute(select(PushToken.user_id, PushToken.platform))).all()
    assert [tuple(r) for r in rows] == [(user.id, "ios")]

    other = await make_user(db_session, "same.phone@example.com")
    other_id = other.id
    await register(client, headers_for(other))  # someone else signs in on this phone
    db_session.expire_all()
    assert (await db_session.execute(select(PushToken.user_id))).scalar_one() == other_id


@pytest.mark.asyncio
async def test_register_validation(client, customer):
    _, h = customer
    assert (await register(client, h, token="not-an-expo-token")).status_code == 400
    assert (await register(client, h, platform="web")).status_code == 422
    assert (await client.post("/api/v1/me/push-tokens", json={"token": TOKEN, "platform": "ios"})).status_code == 401


@pytest.mark.asyncio
async def test_remove_on_logout_only_your_own(client, customer, db_session):
    _, h = customer
    await register(client, h)
    stranger = await make_user(db_session, "not.mine.push@example.com")
    assert (await client.delete(f"/api/v1/me/push-tokens/{TOKEN}", headers=headers_for(stranger))).status_code == 204
    assert (await db_session.execute(select(PushToken))).first() is not None
    assert (await client.delete(f"/api/v1/me/push-tokens/{TOKEN}", headers=h)).status_code == 204
    db_session.expire_all()
    assert (await db_session.execute(select(PushToken))).first() is None


# ─── Sending ─────────────────────────────────────────────────


@pytest.fixture
def expo(monkeypatch):
    """A fake Expo endpoint; tokens listed in `dead` come back as DeviceNotRegistered."""
    state = {"sent": [], "dead": set()}

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)
        state["sent"].extend(batch)
        return httpx.Response(200, json={"data": [
            {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}} if m["to"] in state["dead"]
            else {"status": "ok", "id": "ticket"} for m in batch
        ]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(settings, "EXPO_PUSH_URL", "https://expo.test/push/send")
    monkeypatch.setattr(settings, "NOTIFY_PUSH_ENABLED", True)
    monkeypatch.setattr(push.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return state


@pytest.fixture
def worker_db(monkeypatch, db_session):
    discover_tasks()
    monkeypatch.setattr(registry, "AsyncSessionLocal", async_sessionmaker(bind=db_session.bind))


@pytest.mark.asyncio
async def test_send_push_drops_unregistered_tokens(client, customer, db_session, expo, worker_db):
    user, h = customer
    gone = "ExponentPushToken[goneGONEgoneGONEgone00]"
    await register(client, h)
    await register(client, h, token=gone)
    expo["dead"].add(gone)

    await TASKS["notifications.send_push"].run({"user_id": str(user.id), "title": "Hi", "body": "Test", "data": {"type": "test"}})
    assert {m["to"] for m in expo["sent"]} == {TOKEN, gone}
    assert expo["sent"][0]["data"] == {"type": "test"}
    db_session.expire_all()
    assert (await db_session.execute(select(PushToken.token))).scalars().all() == [TOKEN]


@pytest.mark.asyncio
async def test_no_url_means_log_only(customer, db_session, worker_db, monkeypatch):
    user, _ = customer
    db_session.add(PushToken(user_id=user.id, token=TOKEN, platform="ios"))
    await db_session.commit()
    monkeypatch.setattr(settings, "EXPO_PUSH_URL", "")
    await TASKS["notifications.send_push"].run({"user_id": str(user.id), "title": "Hi", "body": "Test", "data": {}})


@pytest.mark.asyncio
async def test_booking_pushes_go_to_the_right_person(client, salon, customer, db_session, expo, worker_db):
    user, h = customer
    await register(client, h)
    stylist = await make_user(db_session, "push.stylist@example.com")
    staff_token = "ExponentPushToken[staffSTAFFstaffSTAFF0]"
    await register(client, headers_for(stylist), token=staff_token)
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id = stylist.id
    await db_session.commit()
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(days=2))

    await TASKS["booking.push_update"].run({"booking_id": str(b.id), "kind": "confirmed"})
    await TASKS["booking.push_update"].run({"booking_id": str(b.id), "kind": "assigned"})
    to_customer, to_staff = expo["sent"]
    assert (to_customer["to"], to_customer["title"], to_customer["data"]["type"]) == (TOKEN, "Booking confirmed", "booking_confirmed")
    assert to_customer["data"]["bookingId"] == str(b.id) and b.reference in to_customer["body"]
    assert (to_staff["to"], to_staff["title"]) == (staff_token, "New booking")
    assert "Amina" in to_staff["body"]


@pytest.mark.asyncio
async def test_payout_answer_notifies_the_staff_member(client, auth_headers, salon, db_session, monkeypatch):
    import app.modules.notifications.tasks as notification_tasks
    from tests.test_payouts import paid_visit

    calls = []

    async def record(user_id, title, body, **data):
        calls.append((user_id, title, data.get("type")))

    monkeypatch.setattr(notification_tasks, "notify", record)
    user = await make_user(db_session, "payout.push@example.com")
    member = await db_session.get(Staff, uuid.UUID(salon["a"]))
    member.user_id, member.commission_percent = user.id, 40
    await db_session.commit()
    await paid_visit(client, auth_headers, salon)
    payout = (await client.post("/api/v1/payouts", json={"businessId": salon["id"], "amount": 100}, headers=headers_for(user))).json()
    await client.post(f"/api/v1/payouts/{payout['payoutId']}/respond", json={"action": "approve"}, headers=auth_headers)
    owner_id = (await db_session.get(Business, uuid.UUID(salon["id"]))).owner_id
    # The owner hears about the request, the staff member about the answer.
    assert calls == [(owner_id, "Payout request", "payout_requested"), (user.id, "Payout approved", "payout_responded")]
