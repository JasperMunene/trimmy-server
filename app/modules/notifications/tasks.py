import logging
import uuid as uuid_pkg
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.registry import PermanentError, periodic, task

# Registers notifications.dispatch / notifications.send_email with the worker (tasks are
# discovered from tasks.py modules).
from app.modules.notifications import dispatch as _dispatch  # noqa: E402,F401

logger = logging.getLogger(__name__)


@task("notifications.send_push", queue="default", max_retries=3)
async def send_push(user_id: str, title: str, body: str, data: dict, db: AsyncSession) -> None:
    """Push to every device of one user; forget the tokens Expo says are gone."""
    from app.modules.notifications.models import PushToken
    from app.modules.notifications.push import message, send_expo

    try:
        uid = uuid_pkg.UUID(user_id)
    except ValueError:
        raise PermanentError(f"Bad user id {user_id!r}")
    tokens = (await db.execute(select(PushToken.token).where(PushToken.user_id == uid))).scalars().all()
    dead = await send_expo([message(t, title, body, data) for t in tokens])
    if dead:
        await db.execute(delete(PushToken).where(PushToken.token.in_(dead)))
        await db.commit()
        logger.info(f"Removed {len(dead)} unregistered push token(s)")


async def notify(user_id: Optional[uuid_pkg.UUID], title: str, body: str, **data) -> None:
    """Notify one user (no-op for None): inbox, live stream and, when switched on, push.
    `type` goes in data, e.g. notify(uid, "Payout approved", "...", type="payout_responded", payoutId=...).
    Never fails the caller."""
    from app.modules.notifications.dispatch import notify_users

    type_ = data.pop("type", "general")
    await notify_users([user_id], title, body, type=str(type_), data=data)


@periodic("notifications.prune", every=3600)
async def prune_notifications(db: AsyncSession) -> None:
    """Delete inbox rows older than NOTIFICATION_RETENTION_DAYS, a batch at a time so a large
    backlog never holds a long lock."""
    from datetime import datetime, timedelta, timezone

    from app.core.config import settings
    from app.modules.notifications.models import Notification

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.NOTIFICATION_RETENTION_DAYS)
    removed = 0
    for _ in range(20):
        ids = (await db.execute(select(Notification.id).where(Notification.created_at < cutoff).limit(1000))).scalars().all()
        if not ids:
            break
        await db.execute(delete(Notification).where(Notification.id.in_(ids)))
        await db.commit()
        removed += len(ids)
    if removed:
        logger.info(f"Pruned {removed} old notification(s)")


@task("notifications.send_sms", queue="sms", max_retries=5)
async def send_sms_task(to: str, message: str) -> None:
    """A one-off SMS (e.g. to the owner about a submitted day)."""
    import asyncio

    from app.modules.auth.sms import deliver_sms

    await asyncio.to_thread(deliver_sms, to, message, "notification")


async def send_sms(to: str, message: str) -> None:
    """Queue an SMS; never fails the caller."""
    try:
        await send_sms_task.enqueue(to=to, message=message)
    except Exception as e:
        logger.error(f"Could not queue SMS to {to}: {e}")
