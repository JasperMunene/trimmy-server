import logging
import uuid as uuid_pkg
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.registry import PermanentError, task

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
    """Queue a push to a user (no-op for None). Never fails the caller over a notification."""
    if user_id is None:
        return
    try:
        await send_push.enqueue(user_id=str(user_id), title=title, body=body,
                                data={k: str(v) for k, v in data.items() if v is not None})
    except Exception as e:
        logger.error(f"Could not queue push to {user_id}: {e}")
