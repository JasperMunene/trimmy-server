"""The one way notifications go out.

    await notify_users([owner_id], "New booking", "Amina booked Braids, Tue 10:00", type="booking_created",
                       data={"bookingId": ..., "businessId": ...})

queues `notifications.dispatch`, which for each person:
  1. writes an inbox row (the source of truth: GET /me/notifications),
  2. sends it to their open apps over the live stream (realtime.py),
  3. pushes it to their phones when NOTIFY_PUSH_ENABLED (Expo → APNs/FCM).

Each call gets an event key when it's queued, so a retried dispatch finds the rows it already
wrote: nobody gets a second inbox entry, stream event or push for one event.

People without an account (e.g. someone invited to a team) can only be reached by SMS or email:
see notify_contact().
"""
import asyncio
import html
import logging
import uuid as uuid_pkg
from datetime import timezone
from typing import Any, Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.workers.registry import PermanentError, task

logger = logging.getLogger(__name__)


def _iso(dt) -> Optional[str]:
    """Always with a UTC offset: a bare timestamp would be read as local time on the phone."""
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def serialize(n) -> dict[str, Any]:
    """A notification as the app sees it (inbox list, stream event)."""
    return {
        "notificationId": str(n.id),
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "data": n.data or {},
        "createdAt": _iso(n.created_at),
        "readAt": _iso(n.read_at),
    }


def _clean_data(data: Optional[dict]) -> dict[str, str]:
    """Ids and dates as strings: the app reads them straight into routes."""
    return {k: str(v) for k, v in (data or {}).items() if v is not None}


async def deliver(
    db: AsyncSession, user_ids: Iterable[Any], *, type: str, title: str, body: str, data: dict, event_key: str,
    sms: Optional[str] = None, email: Optional[dict] = None,
) -> list:
    """Write, stream and send one notification to each person: always the inbox and the live stream;
    push, SMS (`sms` text) and email (`email` = {"subject", "html"}) as far as the channel switches
    and each person's settings (preferences.py) allow. Safe to call again with the same event_key
    (a retry): rows already written are reused and nothing already sent is sent again."""
    from app.modules.notifications.models import Notification
    from app.modules.notifications import realtime

    ids = list(dict.fromkeys(uuid_pkg.UUID(str(u)) for u in user_ids if u))
    if not ids:
        return []
    title, body = title[:200], body[:1000]

    async def existing() -> dict:
        rows = (await db.execute(
            select(Notification).where(Notification.event_key == event_key, Notification.user_id.in_(ids))
        )).scalars().all()
        return {r.user_id: r for r in rows}

    rows = await existing()
    new_ids: set = set()
    missing = [uid for uid in ids if uid not in rows]
    if missing:
        db.add_all(Notification(user_id=uid, type=type, title=title, body=body, data=data, event_key=event_key)
                   for uid in missing)
        try:
            await db.commit()
        except IntegrityError:
            # Another worker ran the same event at the same moment; theirs stands.
            await db.rollback()
        else:
            new_ids = set(missing)
        rows = await existing()  # one read, with server-set values, after the commit

    for uid in new_ids:  # only new rows: the app also de-duplicates by notificationId
        n = rows[uid]
        await realtime.publish(n.user_id, "notification", serialize(n), str(n.id))

    await _send_extras(db, list(rows.values()), type, sms, email)
    return list(rows.values())


async def _send_extras(db: AsyncSession, notifications: list, type: str, sms: Optional[str], email: Optional[dict]) -> None:
    """Push now; queue SMS and email (their own queues and retries), per each person's settings."""
    from app.modules.auth.models import User
    from app.modules.notifications.models import NotificationDelivery
    from app.modules.notifications.preferences import prefs_for

    want_sms = bool(sms) and settings.NOTIFY_SMS_ENABLED
    want_email = bool(email) and settings.NOTIFY_EMAIL_ENABLED
    if not notifications or not (settings.NOTIFY_PUSH_ENABLED or want_sms or want_email):
        return
    # Plain ids up front: pushing commits, which expires the ORM rows.
    pairs = [(n.id, n.user_id) for n in notifications]
    prefs = await prefs_for(db, [uid for _, uid in pairs])
    if settings.NOTIFY_PUSH_ENABLED:
        await _push(db, [n for n in notifications if prefs[n.user_id].allows(type, "push")])
    if not (want_sms or want_email):
        return

    contacts = {uid: (phone, address) for uid, phone, address in (await db.execute(
        select(User.id, User.phone, User.email).where(User.id.in_([uid for _, uid in pairs]))
    )).all()}
    sent = set((await db.execute(
        select(NotificationDelivery.notification_id, NotificationDelivery.channel).where(
            NotificationDelivery.notification_id.in_([nid for nid, _ in pairs]),
            NotificationDelivery.status == "sent",
        )
    )).all())
    for nid, uid in pairs:
        phone, address = contacts.get(uid, (None, None))
        p = prefs[uid]
        try:
            if want_sms and phone and p.allows(type, "sms") and (nid, "sms") not in sent:
                await deliver_sms_task.enqueue(notification_id=str(nid), to=phone, text=sms)
            if want_email and address and p.allows(type, "email") and (nid, "email") not in sent:
                await deliver_email_task.enqueue(notification_id=str(nid), to=address,
                                                 subject=email["subject"], html_body=email["html"])
        except Exception as e:  # the inbox already has it
            logger.error(f"Could not queue SMS/email for notification {nid}: {e}")


async def _send_once(db: AsyncSession, notification_id: str, channel: str, send) -> None:
    """Claim (notification, channel), send, record. A claim already marked sent is skipped, so a
    retried or duplicated task never sends twice."""
    from app.modules.notifications.models import NotificationDelivery

    try:
        nid = uuid_pkg.UUID(notification_id)
    except ValueError:
        raise PermanentError(f"Bad notification id {notification_id!r}")

    async def current():
        return (await db.execute(select(NotificationDelivery).where(
            NotificationDelivery.notification_id == nid, NotificationDelivery.channel == channel
        ))).scalar_one_or_none()

    row = await current()
    if row is None:
        db.add(NotificationDelivery(notification_id=nid, channel=channel, status="sending"))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
        row = await current()
        if row is None:
            return  # the notification itself was deleted
    if row.status == "sent":
        return
    try:
        await send()
    except Exception as e:
        row.status, row.detail = "failed", str(e)[:500]
        await db.commit()
        raise
    row.status, row.detail = "sent", None
    await db.commit()


@task("notifications.deliver_sms", queue="sms", max_retries=5, inline_fallback=True)
async def deliver_sms_task(notification_id: str, to: str, text: str, db: AsyncSession) -> None:
    from app.modules.auth.sms import deliver_sms

    await _send_once(db, notification_id, "sms", lambda: asyncio.to_thread(deliver_sms, to, text, "notification"))


@task("notifications.deliver_email", queue="emails", max_retries=5, inline_fallback=True)
async def deliver_email_task(notification_id: str, to: str, subject: str, html_body: str, db: AsyncSession) -> None:
    from app.modules.auth.emails import deliver as deliver_email

    await _send_once(db, notification_id, "email",
                     lambda: asyncio.to_thread(deliver_email, to, subject, html_body, "notification"))


async def _push(db: AsyncSession, notifications: list) -> None:
    """Push the notifications that haven't been pushed yet. Raises on a provider error so the task
    retries; what already went out is recorded and not sent twice."""
    from app.modules.notifications.models import NotificationDelivery, PushToken
    from app.modules.notifications.push import message, send_expo

    if not notifications:
        return
    done = set((await db.execute(
        select(NotificationDelivery.notification_id).where(
            NotificationDelivery.notification_id.in_([n.id for n in notifications]),
            NotificationDelivery.channel == "push", NotificationDelivery.status == "sent",
        )
    )).scalars().all())
    pending = [n for n in notifications if n.id not in done]
    if not pending:
        return
    tokens: dict = {}
    for user_id, token in (await db.execute(
        select(PushToken.user_id, PushToken.token).where(PushToken.user_id.in_({n.user_id for n in pending}))
    )).all():
        tokens.setdefault(user_id, []).append(token)

    messages, sent = [], []
    for n in pending:
        user_tokens = tokens.get(n.user_id, [])
        if not user_tokens:
            continue  # no device registered: the inbox and the stream still have it
        payload = {**(n.data or {}), "type": n.type, "notificationId": str(n.id)}
        messages += [message(t, n.title, n.body, payload) for t in user_tokens]
        sent.append(n)
    if not messages:
        return
    dead = await send_expo(messages)
    db.add_all(NotificationDelivery(notification_id=n.id, channel="push", status="sent") for n in sent)
    if dead:
        await db.execute(delete(PushToken).where(PushToken.token.in_(dead)))
        logger.info(f"Removed {len(dead)} unregistered push token(s)")
    await db.commit()


@task("notifications.dispatch", queue="default", max_retries=5, inline_fallback=True)
async def dispatch(user_ids: list[str], type: str, title: str, body: str, data: dict, event_key: str,
                   db: AsyncSession, sms: Optional[str] = None, email: Optional[dict] = None) -> None:
    if not event_key:
        raise PermanentError("dispatch needs an event_key")
    await deliver(db, user_ids, type=type, title=title, body=body, data=data, event_key=event_key, sms=sms, email=email)


async def notify_users(
    user_ids: Iterable[Any], title: str, body: str, *, type: str, data: Optional[dict] = None,
    exclude: Any = None, event_key: Optional[str] = None, sms: Optional[str] = None, email: Optional[dict] = None,
) -> None:
    """Queue one notification to several people (None entries and `exclude`, usually whoever caused
    it, are skipped). Call after the commit that made the change. Never fails the caller."""
    ids = [str(u) for u in dict.fromkeys(user_ids) if u and u != exclude]
    if not ids:
        return
    try:
        await dispatch.enqueue(user_ids=ids, type=type, title=title, body=body, data=_clean_data(data),
                               event_key=event_key or uuid_pkg.uuid4().hex, sms=sms, email=email)
    except Exception as e:
        logger.error(f"Could not queue {type} notification to {len(ids)} user(s): {e}")


# ─── People without an account: SMS / email only ─────────────


@task("notifications.send_email", queue="emails", max_retries=5)
async def send_email_task(to: str, subject: str, html_body: str) -> None:
    from app.modules.auth.emails import deliver as deliver_email

    await asyncio.to_thread(deliver_email, to, subject, html_body, "notification")


def email_html(heading: str, paragraphs: list[str], link: Optional[str] = None, link_label: str = "Open Trimmy") -> str:
    """A plain, mobile-friendly email body (escaped; no images, so it loads on any connection)."""
    parts = [f"<h2>{html.escape(heading)}</h2>"] + [f"<p>{html.escape(p)}</p>" for p in paragraphs]
    if link:
        parts.append(
            f'<p><a href="{html.escape(link, quote=True)}" style="display:inline-block;padding:12px 20px;'
            f'background:#222;color:#fff;border-radius:999px;text-decoration:none">{html.escape(link_label)}</a></p>'
        )
    return "\n".join(parts)


async def notify_contact(
    *, phone: Optional[str] = None, email: Optional[str] = None, sms: Optional[str] = None,
    subject: Optional[str] = None, html_body: Optional[str] = None,
) -> None:
    """Text and/or email someone who isn't a user yet, as far as the channel switches allow.
    Never fails the caller."""
    from app.modules.notifications.tasks import send_sms_task

    try:
        if phone and sms and settings.NOTIFY_SMS_ENABLED:
            await send_sms_task.enqueue(to=phone, message=sms)
        if email and subject and html_body and settings.NOTIFY_EMAIL_ENABLED:
            await send_email_task.enqueue(to=email, subject=subject, html_body=html_body)
    except Exception as e:
        logger.error(f"Could not queue a message to a contact: {e}")
