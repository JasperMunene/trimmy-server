"""The in-app inbox (GET /me/notifications ...) and the live stream (GET /me/events)."""
import asyncio
import base64
import logging
import time
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.core.dependencies import get_current_user
from app.core.rate_limit import limiter
from app.core.security import decode_token
from app.modules.auth.models import User
from app.modules.notifications.dispatch import deliver, serialize
from app.modules.notifications.models import Notification
from app.modules.notifications.realtime import RESYNC, hub, sse

logger = logging.getLogger(__name__)

inbox_router = APIRouter(prefix="/me/notifications", tags=["Notifications"])
events_router = APIRouter(prefix="/me/events", tags=["Notifications"])

MAX_PAGE = 50
CATCH_UP_LIMIT = 50  # more missed than this and the app is told to refetch instead


class NotificationItem(BaseModel):
    notificationId: str
    type: str
    title: str
    body: str
    data: dict[str, Any]
    createdAt: Optional[str]
    readAt: Optional[str]


class NotificationPageResponse(BaseModel):
    notifications: list[NotificationItem]
    unreadCount: int
    nextCursor: Optional[str]


class UnreadCountResponse(BaseModel):
    unreadCount: int


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _encode_cursor(n: Notification) -> str:
    raw = f"{_aware(n.created_at).isoformat()}|{n.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid_pkg.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, nid = raw.split("|", 1)
        return _aware(datetime.fromisoformat(at)), uuid_pkg.UUID(nid)
    except (ValueError, UnicodeDecodeError):
        raise _error(400, "validation_error", "Invalid cursor")


async def _unread_count(db: AsyncSession, user_id: uuid_pkg.UUID) -> int:
    return (await db.execute(
        select(func.count()).select_from(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )).scalar_one()


@inbox_router.get("", response_model=NotificationPageResponse)
async def list_notifications(
    cursor: Optional[str] = None,
    limit: int = Query(20, ge=1, le=MAX_PAGE),
    unread: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Newest first. unreadCount always counts every unread row, so one call refreshes the bell."""
    query = select(Notification).where(Notification.user_id == current_user.id)
    if unread:
        query = query.where(Notification.read_at.is_(None))
    if cursor:
        at, nid = _decode_cursor(cursor)
        query = query.where(or_(Notification.created_at < at, and_(Notification.created_at == at, Notification.id < nid)))
    rows = (await db.execute(
        query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit + 1)
    )).scalars().all()
    page = rows[:limit]
    return NotificationPageResponse(
        notifications=[NotificationItem(**serialize(n)) for n in page],
        unreadCount=await _unread_count(db, current_user.id),
        nextCursor=_encode_cursor(page[-1]) if len(rows) > limit else None,
    )


@inbox_router.get("/unread-count", response_model=UnreadCountResponse)
async def unread_count(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return UnreadCountResponse(unreadCount=await _unread_count(db, current_user.id))


@inbox_router.post("/read-all", response_model=UnreadCountResponse)
async def mark_all_read(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return UnreadCountResponse(unreadCount=0)


@inbox_router.post("/{notification_id}/read", response_model=UnreadCountResponse)
async def mark_read(notification_id: str, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Idempotent. Someone else's notification is 404."""
    try:
        nid = uuid_pkg.UUID(notification_id)
    except ValueError:
        raise _error(404, "not_found", "Notification not found")
    n = await db.get(Notification, nid)
    if n is None or n.user_id != current_user.id:
        raise _error(404, "not_found", "Notification not found")
    if n.read_at is None:
        n.read_at = datetime.now(timezone.utc)
        await db.commit()
    return UnreadCountResponse(unreadCount=await _unread_count(db, current_user.id))


@inbox_router.delete("/{notification_id}", response_model=UnreadCountResponse)
async def delete_notification(notification_id: str, current_user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    """Clear one from the inbox (the app's swipe to delete). Someone else's, or one already gone, is 404."""
    try:
        nid = uuid_pkg.UUID(notification_id)
    except ValueError:
        raise _error(404, "not_found", "Notification not found")
    n = await db.get(Notification, nid)
    if n is None or n.user_id != current_user.id:
        raise _error(404, "not_found", "Notification not found")
    await db.delete(n)
    await db.commit()
    return UnreadCountResponse(unreadCount=await _unread_count(db, current_user.id))


@inbox_router.post("/test", response_model=NotificationItem)
@limiter.limit("5/minute")
async def send_test_notification(request: Request, current_user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """Send yourself a notification through the whole pipeline (inbox, stream, push when on)."""
    if not settings.NOTIFY_TEST_ENDPOINT_ENABLED:
        raise _error(404, "not_found", "Not found")
    rows = await deliver(db, [current_user.id], type="test", title="Test notification",
                         body="Notifications are working on this device.", data={},
                         event_key=f"test:{uuid_pkg.uuid4().hex}")
    return NotificationItem(**serialize(rows[0]))


# ─── Live stream ─────────────────────────────────────────────


def _stream_user_id(request: Request) -> uuid_pkg.UUID:
    """Bearer auth checked once, when the stream opens; the stream closes before the token would
    have expired for long (SSE_MAX_CONNECTION_SECONDS) and the app reconnects with a fresh one.
    Done by hand so an open stream never holds a database session."""
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    payload = decode_token(token) if token else None
    if payload is None or payload.get("type") != "access" or not payload.get("sub"):
        raise _error(401, "invalid_token", "Invalid or expired token")
    try:
        return uuid_pkg.UUID(str(payload["sub"]))
    except ValueError:
        raise _error(401, "invalid_token", "Invalid token payload")


async def _missed_since(user_id: uuid_pkg.UUID, after: tuple[datetime, uuid_pkg.UUID]) -> list[Notification]:
    """Notifications newer than `after`, oldest first (a short-lived session)."""
    at, nid = after
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(Notification)
            .where(Notification.user_id == user_id,
                   or_(Notification.created_at > at, and_(Notification.created_at == at, Notification.id > nid)))
            .order_by(Notification.created_at, Notification.id)
            .limit(CATCH_UP_LIMIT + 1)
        )).scalars().all())


async def _event_stream(request: Request, user_id: uuid_pkg.UUID,
                        after: Optional[tuple[datetime, uuid_pkg.UUID]], resync: bool) -> AsyncIterator[str]:
    key = str(user_id)
    # Opened here, not in the endpoint, so the finally below always closes it.
    queue = hub.open(key)
    if queue is None:
        yield sse("reconnect", {})
        return
    try:
        # Reconnect hint for clients that honour it; the app has its own backoff.
        yield "retry: 3000\n\n"
        yield sse("ready", {"heartbeatSeconds": settings.SSE_HEARTBEAT_SECONDS})
        if resync:
            yield sse("resync", {})

        async def catch_up() -> AsyncIterator[str]:
            nonlocal after
            missed = await _missed_since(user_id, after)
            if len(missed) > CATCH_UP_LIMIT:
                after = (_aware(missed[-1].created_at), missed[-1].id)
                yield sse("resync", {})
                return
            for n in missed:
                after = (_aware(n.created_at), n.id)
                yield sse("notification", serialize(n), str(n.id))

        if after is not None:
            async for chunk in catch_up():
                yield chunk
        else:
            after = (datetime.now(timezone.utc), uuid_pkg.UUID(int=0))

        deadline = time.monotonic() + settings.SSE_MAX_CONNECTION_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield sse("reconnect", {})
                return
            try:
                message = await asyncio.wait_for(queue.get(), timeout=min(settings.SSE_HEARTBEAT_SECONDS, remaining))
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    return
                if not hub.healthy:
                    # Redis is down: events from workers can't reach us, so read the inbox instead.
                    async for chunk in catch_up():
                        yield chunk
                yield ": ping\n\n"
                continue
            if message is RESYNC or message.get("event") == "resync":
                yield sse("resync", {})
                continue
            data = message.get("data") or {}
            if message.get("event") == "notification" and data.get("createdAt") and data.get("notificationId"):
                try:
                    after = max(after, (_aware(datetime.fromisoformat(data["createdAt"])), uuid_pkg.UUID(data["notificationId"])))
                except ValueError:
                    pass
            yield sse(message.get("event", "message"), data, message.get("id"))
    finally:
        hub.close(key, queue)


@events_router.get(
    "", response_class=StreamingResponse,
    responses={200: {"description": "Server-sent events", "content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def event_stream(request: Request, last_event_id: Optional[str] = Query(None, alias="lastEventId")):
    """Server-sent events for the signed-in user:

    - `notification`: a new inbox entry (same shape as GET /me/notifications items; `id:` is its notificationId)
    - `resync`: events may have been missed; refetch the inbox (and whatever is on screen)
    - `reconnect`: the server is closing this stream; open a new one (with Last-Event-ID)
    - `ready`, plus a `: ping` comment every SSE_HEARTBEAT_SECONDS

    Send `Last-Event-ID` (header, or `?lastEventId=`) when reconnecting to receive what was missed.
    """
    user_id = _stream_user_id(request)
    async with AsyncSessionLocal() as db:
        if await db.get(User, user_id) is None:
            raise _error(401, "user_not_found", "User no longer exists")
        last_id = request.headers.get("last-event-id") or last_event_id
        after, resync = None, False
        if last_id:
            try:
                last = await db.get(Notification, uuid_pkg.UUID(last_id))
            except ValueError:
                last = None
            if last is not None and last.user_id == user_id:
                after = (_aware(last.created_at), last.id)
            else:
                resync = True  # pruned or unknown: the app refetches instead

    if hub.stream_count(str(user_id)) >= settings.SSE_MAX_STREAMS_PER_USER:
        raise _error(429, "too_many_streams", "Too many open connections for this account")
    return StreamingResponse(
        _event_stream(request, user_id, after, resync),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
