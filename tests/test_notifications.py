import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.modules.notifications.inbox as inbox
import app.modules.notifications.tasks as notification_tasks
from app.core.config import settings
from app.modules.business.models import Staff
from app.modules.notifications.dispatch import deliver
from app.modules.notifications.models import Notification, NotificationDelivery, PushToken
from app.modules.notifications.realtime import hub
from app.workers.registry import TASKS
from tests.test_create_booking import customer, salon  # noqa: F401  (fixtures)
from tests.test_push import TOKEN, expo, worker_db  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, insert_booking, make_user


async def send(db, user_ids, key="evt-1", type="test", title="Hello", body="World", data=None, sms=None, email=None):
    return await deliver(db, user_ids, type=type, title=title, body=body, data=data or {}, event_key=key,
                         sms=sms, email=email)


# ─── deliver(): inbox, stream, push ──────────────────────────


@pytest.mark.asyncio
async def test_deliver_writes_one_row_per_person_and_a_retry_adds_nothing(db_session, test_user):
    other = await make_user(db_session, "second.inbox@example.com")
    queue = hub.open(str(test_user.id))
    try:
        rows = await send(db_session, [test_user.id, other.id, test_user.id, None])
        assert len(rows) == 2
        event = queue.get_nowait()
        assert event["event"] == "notification" and event["data"]["title"] == "Hello"
        assert event["id"] == event["data"]["notificationId"]

        await send(db_session, [test_user.id, other.id])  # the task retried
        assert queue.empty()  # no second stream event either
    finally:
        hub.close(str(test_user.id), queue)
    count = (await db_session.execute(select(func.count()).select_from(Notification))).scalar_one()
    assert count == 2


@pytest.mark.asyncio
async def test_push_is_off_by_default(db_session, test_user, expo, monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_PUSH_ENABLED", False)
    db_session.add(PushToken(user_id=test_user.id, token=TOKEN, platform="ios"))
    await db_session.commit()
    await send(db_session, [test_user.id])
    assert expo["sent"] == []


@pytest.mark.asyncio
async def test_push_when_on_goes_once_even_if_retried(db_session, test_user, expo):
    db_session.add(PushToken(user_id=test_user.id, token=TOKEN, platform="ios"))
    await db_session.commit()
    await send(db_session, [test_user.id], data={"bookingId": "b1"})
    await send(db_session, [test_user.id], data={"bookingId": "b1"})
    assert len(expo["sent"]) == 1
    data = expo["sent"][0]["data"]
    assert data["type"] == "test" and data["bookingId"] == "b1" and data["notificationId"]
    assert (await db_session.execute(select(NotificationDelivery.status))).scalars().all() == ["sent"]


# ─── Inbox endpoints ─────────────────────────────────────────


async def seed(db: AsyncSession, user_id, n: int):
    start = datetime.now(timezone.utc) - timedelta(hours=n)
    for i in range(n):
        db.add(Notification(user_id=user_id, type="test", title=f"#{i}", body="b", data={}, event_key=f"seed-{i}",
                            created_at=start + timedelta(minutes=i)))
    await db.commit()


@pytest.mark.asyncio
async def test_list_pages_newest_first_and_counts_unread(client, auth_headers, db_session, test_user):
    await seed(db_session, test_user.id, 5)
    first = (await client.get("/api/v1/me/notifications?limit=2", headers=auth_headers)).json()
    assert [n["title"] for n in first["notifications"]] == ["#4", "#3"]
    assert first["unreadCount"] == 5 and first["nextCursor"]
    second = (await client.get(f"/api/v1/me/notifications?limit=2&cursor={first['nextCursor']}", headers=auth_headers)).json()
    assert [n["title"] for n in second["notifications"]] == ["#2", "#1"]
    third = (await client.get(f"/api/v1/me/notifications?limit=2&cursor={second['nextCursor']}", headers=auth_headers)).json()
    assert [n["title"] for n in third["notifications"]] == ["#0"] and third["nextCursor"] is None
    bad = await client.get("/api/v1/me/notifications?cursor=nope", headers=auth_headers)
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_mark_read_one_and_all(client, auth_headers, db_session, test_user):
    await seed(db_session, test_user.id, 3)
    newest = (await client.get("/api/v1/me/notifications", headers=auth_headers)).json()["notifications"][0]
    url = f"/api/v1/me/notifications/{newest['notificationId']}/read"
    assert (await client.post(url, headers=auth_headers)).json() == {"unreadCount": 2}
    assert (await client.post(url, headers=auth_headers)).json() == {"unreadCount": 2}  # idempotent
    unread = (await client.get("/api/v1/me/notifications?unread=true", headers=auth_headers)).json()
    assert len(unread["notifications"]) == 2

    stranger = await make_user(db_session, "not.my.inbox@example.com")
    assert (await client.post(url, headers=headers_for(stranger))).status_code == 404
    assert (await client.post("/api/v1/me/notifications/nonsense/read", headers=auth_headers)).status_code == 404

    assert (await client.post("/api/v1/me/notifications/read-all", headers=auth_headers)).json() == {"unreadCount": 0}
    assert (await client.get("/api/v1/me/notifications/unread-count", headers=auth_headers)).json() == {"unreadCount": 0}


@pytest.mark.asyncio
async def test_delete_one(client, auth_headers, db_session, test_user):
    await seed(db_session, test_user.id, 2)
    newest = (await client.get("/api/v1/me/notifications", headers=auth_headers)).json()["notifications"][0]
    url = f"/api/v1/me/notifications/{newest['notificationId']}"
    stranger = await make_user(db_session, "not.my.inbox.delete@example.com")
    assert (await client.delete(url, headers=headers_for(stranger))).status_code == 404
    assert (await client.delete(url, headers=auth_headers)).json() == {"unreadCount": 1}
    assert (await client.delete(url, headers=auth_headers)).status_code == 404
    left = (await client.get("/api/v1/me/notifications", headers=auth_headers)).json()["notifications"]
    assert len(left) == 1 and left[0]["notificationId"] != newest["notificationId"]


@pytest.mark.asyncio
async def test_test_notification_reaches_the_inbox(client, auth_headers, monkeypatch):
    sent = await client.post("/api/v1/me/notifications/test", headers=auth_headers)
    assert sent.status_code == 200 and sent.json()["type"] == "test"
    assert (await client.get("/api/v1/me/notifications/unread-count", headers=auth_headers)).json() == {"unreadCount": 1}
    monkeypatch.setattr(settings, "NOTIFY_TEST_ENDPOINT_ENABLED", False)
    assert (await client.post("/api/v1/me/notifications/test", headers=auth_headers)).status_code == 404


# ─── Live stream ─────────────────────────────────────────────


class FakeRequest:
    async def is_disconnected(self):
        return False


async def next_event(stream) -> dict:
    """The next real event from the stream (skipping the retry hint and pings)."""
    while True:
        chunk = await asyncio.wait_for(stream.__anext__(), timeout=2)
        if chunk.startswith((":", "retry:")):
            continue
        fields = dict(line.split(": ", 1) for line in chunk.strip().split("\n"))
        return {"event": fields["event"], "id": fields.get("id"), "data": json.loads(fields["data"])}


@pytest.fixture
def stream_db(monkeypatch, db_session):
    monkeypatch.setattr(inbox, "AsyncSessionLocal", async_sessionmaker(bind=db_session.bind, expire_on_commit=False))


@pytest.mark.asyncio
async def test_stream_delivers_live_events(db_session, test_user, stream_db):
    stream = inbox._event_stream(FakeRequest(), test_user.id, None, False)
    assert (await next_event(stream))["event"] == "ready"
    pending = asyncio.ensure_future(next_event(stream))
    await asyncio.sleep(0)  # the stream is now waiting on its queue
    rows = await send(db_session, [test_user.id], title="Live")
    event = await pending
    assert event["event"] == "notification" and event["data"]["title"] == "Live" and event["id"] == str(rows[0].id)
    await stream.aclose()
    assert hub.stream_count(str(test_user.id)) == 0  # closed streams leave the hub


@pytest.mark.asyncio
async def test_stream_catches_up_after_last_event_id(db_session, test_user, stream_db):
    await seed(db_session, test_user.id, 3)
    oldest = (await db_session.execute(select(Notification).order_by(Notification.created_at))).scalars().first()
    after = (oldest.created_at.replace(tzinfo=timezone.utc), oldest.id)
    stream = inbox._event_stream(FakeRequest(), test_user.id, after, False)
    assert (await next_event(stream))["event"] == "ready"
    assert [(await next_event(stream))["data"]["title"] for _ in range(2)] == ["#1", "#2"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_stream_endpoint_auth_and_too_many_streams(client, auth_headers, monkeypatch, stream_db):
    assert (await client.get("/api/v1/me/events")).status_code == 401
    assert (await client.get("/api/v1/me/events", headers={"Authorization": "Bearer junk"})).status_code == 401
    monkeypatch.setattr(settings, "SSE_MAX_STREAMS_PER_USER", 0)
    assert (await client.get("/api/v1/me/events", headers=auth_headers)).status_code == 429


@pytest.mark.asyncio
async def test_a_slow_stream_is_told_to_resync(test_user):
    queue = hub.open(str(test_user.id))
    try:
        for i in range(150):
            hub.dispatch(str(test_user.id), {"event": "notification", "data": {"n": i}, "id": str(i)})
        assert queue.qsize() == 50  # dropped the backlog at 100, then kept the newer ones
        assert queue.get_nowait()["event"] == "resync"
    finally:
        hub.close(str(test_user.id), queue)


# ─── Who hears about what ────────────────────────────────────


@pytest.mark.asyncio
async def test_booking_events_reach_the_desk_not_the_actor(client, auth_headers, salon, customer, db_session, test_user, worker_db):
    user, _ = customer
    desk = await make_user(db_session, "desk.inbox@example.com")
    db_session.add(Staff(business_id=uuid.UUID(salon["id"]), user_id=desk.id, name="Desk", role="front_desk",
                         performs_all_services=False))
    await db_session.commit()
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(days=2))

    await TASKS["booking.push_update"].run({"booking_id": str(b.id), "kind": "created"})
    await TASKS["booking.push_update"].run({"booking_id": str(b.id), "kind": "cancelled_by_customer",
                                            "actor_id": str(user.id)})
    rows = (await db_session.execute(select(Notification.user_id, Notification.type))).all()
    by_type: dict = {}
    for uid, t in rows:
        by_type.setdefault(t, set()).add(uid)
    assert by_type["booking_created"] == {test_user.id, desk.id}  # owner + front desk
    assert user.id not in by_type["booking_cancelled_by_customer"]  # not the customer who did it


@pytest.mark.asyncio
async def test_inviting_an_existing_user_notifies_them_in_app(client, auth_headers, salon, db_session, monkeypatch):
    calls, contacted = [], []

    async def record(user_id, title, body, **data):
        calls.append((user_id, data.get("type")))

    async def record_contact(**kw):
        contacted.append(kw)

    import app.modules.notifications.dispatch as dispatch_module
    monkeypatch.setattr(notification_tasks, "notify", record)
    monkeypatch.setattr(dispatch_module, "notify_contact", record_contact)
    member = await make_user(db_session, "invitee.inapp@example.com")
    url = f"/api/v1/businesses/{salon['id']}/team/invitations"
    r = await client.post(url, json={"email": "invitee.inapp@example.com", "role": "staff"}, headers=auth_headers)
    assert r.status_code in (200, 201), r.text
    assert calls == [(member.id, "team_invitation")] and contacted == []

    r = await client.post(url, json={"phone": "+254711000999", "role": "staff"}, headers=auth_headers)
    assert r.status_code in (200, 201), r.text
    assert contacted and contacted[0]["phone"] == "+254711000999" and "invited you" in contacted[0]["sms"]


@pytest.mark.asyncio
async def test_prune_drops_old_rows(db_session, test_user, worker_db):
    db_session.add(Notification(user_id=test_user.id, type="test", title="old", body="b", data={}, event_key="old",
                                created_at=datetime.now(timezone.utc) - timedelta(days=settings.NOTIFICATION_RETENTION_DAYS + 1)))
    await db_session.commit()
    await send(db_session, [test_user.id], key="new")
    await TASKS["notifications.prune"].run({})
    db_session.expire_all()
    assert (await db_session.execute(select(Notification.title))).scalars().all() == ["Hello"]


# ─── Settings (GET/PATCH /me/notification-preferences) ───────

PREFS = "/api/v1/me/notification-preferences"


@pytest.mark.asyncio
async def test_settings_show_the_categories_that_apply(client, auth_headers, salon, customer):
    _, cust = customer
    owner = (await client.get(PREFS, headers=auth_headers)).json()  # test_user owns the salon
    assert set(owner["categories"]) == {"bookings", "reminders", "business", "payouts", "team"}
    assert owner["availableChannels"] == {"push": False, "sms": True, "email": True}  # push is off
    assert owner["categories"]["business"] == {"push": True, "sms": False, "email": False}
    assert set((await client.get(PREFS, headers=cust)).json()["categories"]) == {"bookings", "reminders", "team"}


@pytest.mark.asyncio
async def test_patch_a_category_channel_and_reminder_columns_stay_in_step(client, customer):
    _, h = customer
    r = await client.patch(PREFS, json={"categories": {"bookings": {"email": False}, "reminders": {"sms": False},
                                                       "nonsense": {"push": False}}}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["categories"]["bookings"] == {"push": True, "sms": True, "email": False}
    assert body["reminders"]["sms"] is False and body["categories"]["reminders"]["sms"] is False
    again = await client.patch(PREFS, json={"reminders": {"email": True}}, headers=h)
    assert again.json()["categories"]["reminders"]["email"] is True
    assert again.json()["categories"]["bookings"]["email"] is False  # untouched


# ─── SMS / email channels ────────────────────────────────────


@pytest.fixture
def channel_queue(monkeypatch):
    import app.modules.notifications.dispatch as d

    queued = []

    async def sms_enqueue(**kw):
        queued.append(("sms", kw["to"]))

    async def email_enqueue(**kw):
        queued.append(("email", kw["to"]))

    monkeypatch.setattr(d.deliver_sms_task, "enqueue", sms_enqueue)
    monkeypatch.setattr(d.deliver_email_task, "enqueue", email_enqueue)
    return queued


EMAIL = {"subject": "Hi", "html": "<p>Hi</p>"}


@pytest.mark.asyncio
async def test_sms_and_email_follow_settings_and_switches(client, db_session, test_user, auth_headers, channel_queue, monkeypatch):
    await send(db_session, [test_user.id], key="a", type="booking_confirmed", sms="text", email=EMAIL)
    assert sorted(channel_queue) == [("email", "test@example.com"), ("sms", "+1234567890")]

    channel_queue.clear()
    await client.patch(PREFS, json={"categories": {"bookings": {"email": False}}}, headers=auth_headers)
    await send(db_session, [test_user.id], key="b", type="booking_confirmed", sms="text", email=EMAIL)
    assert channel_queue == [("sms", "+1234567890")]

    channel_queue.clear()
    monkeypatch.setattr(settings, "NOTIFY_SMS_ENABLED", False)
    await send(db_session, [test_user.id], key="c", type="booking_confirmed", sms="text", email=EMAIL)
    assert channel_queue == []


async def _deliveries(db_session):
    db_session.expire_all()
    return (await db_session.execute(select(NotificationDelivery.channel, NotificationDelivery.status))).all()


@pytest.mark.asyncio
async def test_a_channel_send_happens_once_and_a_failure_can_retry(db_session, test_user, worker_db, monkeypatch):
    import app.modules.auth.sms as sms_module

    [n] = await send(db_session, [test_user.id], key="once")
    nid = str(n.id)
    texts, fail = [], {"on": True}

    def fake_sms(to, message, log_hint=""):
        if fail["on"]:
            raise RuntimeError("gateway down")
        texts.append(message)

    monkeypatch.setattr(sms_module, "deliver_sms", fake_sms)
    task = TASKS["notifications.deliver_sms"]
    with pytest.raises(RuntimeError):
        await task.run({"notification_id": nid, "to": "+254700000001", "text": "hi"})
    assert [tuple(r) for r in await _deliveries(db_session)] == [("sms", "failed")]

    fail["on"] = False
    await task.run({"notification_id": nid, "to": "+254700000001", "text": "hi"})
    await task.run({"notification_id": nid, "to": "+254700000001", "text": "hi"})  # a duplicate
    assert texts == ["hi"]
    assert [tuple(r) for r in await _deliveries(db_session)] == [("sms", "sent")]


# ─── After-commit hook ───────────────────────────────────────


@pytest.mark.asyncio
async def test_after_commit_runs_only_for_committed_changes(db_session):
    from app.modules.notifications.after_commit import notify_after_commit

    ran = []

    async def note(label):
        ran.append(label)

    notify_after_commit(db_session, lambda: note("rolled back"))
    await db_session.rollback()
    notify_after_commit(db_session, lambda: note("committed"))
    await db_session.commit()
    await asyncio.sleep(0.01)
    assert ran == ["committed"]


# ─── Booking messages ────────────────────────────────────────


@pytest.mark.asyncio
async def test_business_cancel_tells_customer_by_sms_and_email(client, salon, customer, db_session, worker_db, channel_queue):
    user, _ = customer
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(days=2),
                             status="cancelled", refund_amount=300, refund_status="due")
    await TASKS["booking.push_update"].run({"booking_id": str(b.id), "kind": "cancelled_by_business"})
    row = (await db_session.execute(select(Notification).where(Notification.type == "booking_cancelled_by_business"))).scalar_one()
    assert row.user_id == user.id and "refund your KES 300" in row.body
    assert {c for c, _ in channel_queue} == {"sms", "email"}


@pytest.mark.asyncio
async def test_business_reschedule_says_it_moved(salon, customer, db_session, worker_db):
    user, _ = customer
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(days=2))
    await TASKS["booking.push_update"].run({"booking_id": str(b.id), "kind": "rescheduled_by_business",
                                            "extra": {"old_when": "Mon 5 Oct, 10:00"}})
    row = (await db_session.execute(select(Notification))).scalar_one()
    assert row.title == "Booking moved" and "from Mon 5 Oct, 10:00" in row.body


@pytest.mark.asyncio
async def test_completed_is_notified_once_however_it_happens(salon, customer, db_session, worker_db):
    from app.modules.booking import tasks as booking_tasks

    user, _ = customer
    b = await insert_booking(db_session, salon, user.id, datetime.now(timezone.utc) + timedelta(days=2))
    for _ in range(2):  # e.g. the status stepper, then the charge
        await TASKS["booking.push_update"].run({"booking_id": str(b.id), "kind": "completed",
                                                "event_key": f"booking:{b.id}:completed"})
    assert "completed" in booking_tasks.ONCE_KINDS
    assert (await db_session.execute(select(func.count()).select_from(Notification))).scalar_one() == 1


@pytest.mark.asyncio
async def test_guest_bookings_get_email_without_an_account(salon, db_session, worker_db, monkeypatch):
    import app.modules.notifications.dispatch as d

    contacted = []

    async def record(**kw):
        contacted.append(kw)

    monkeypatch.setattr(d, "notify_contact", record)
    b = await insert_booking(db_session, salon, None, datetime.now(timezone.utc) + timedelta(days=2),
                             customer_email="guest@example.com")
    await TASKS["booking.push_update"].run({"booking_id": str(b.id), "kind": "confirmed"})
    assert contacted and contacted[0]["email"] == "guest@example.com" and "Booking confirmed" in contacted[0]["subject"]


@pytest.mark.asyncio
async def test_phoneless_customers_get_reminders_in_the_app(salon, customer, db_session, worker_db, monkeypatch):
    from app.modules.booking import tasks as booking_tasks

    user, _ = customer
    calls = []

    async def record(booking_id, kind, *a, **kw):
        calls.append(kind)

    monkeypatch.setattr(booking_tasks, "push_update", record)
    now = datetime.now(timezone.utc)
    b = await insert_booking(db_session, salon, user.id, now + timedelta(hours=23))  # no customer_phone
    b.created_at = now - timedelta(days=3)
    await db_session.commit()

    async def run_now(**kw):
        await TASKS["booking.send_reminder"].run(kw)

    monkeypatch.setattr(booking_tasks.send_booking_reminder, "enqueue", run_now)
    await TASKS["booking.scan_reminders"].run({})
    assert calls == ["reminder_24h"]
