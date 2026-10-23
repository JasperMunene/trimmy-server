"""Shared ticket logic for the people who ask (router.py) and the team (admin.py)."""
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.images import PHOTO_SIDE, ImageError, shrink_image
from app.core.storage import get_storage, sniff_image
from app.modules.auth.models import User
from app.modules.support.models import MessageKind, SupportTicket, TicketMessage, TicketStatus
from app.modules.support.schemas import MessageOut, TicketRow

PREVIEW = 140
MAX_BODY = 5000


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def person_name(user: Optional[User]) -> str:
    if user is None:
        return ""
    return " ".join(p for p in (user.first_name, user.last_name) if p) or user.email or user.phone or "Customer"


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(at: Optional[datetime]) -> Optional[datetime]:
    """Always with a time zone in responses (SQLite hands datetimes back without one)."""
    return at.replace(tzinfo=timezone.utc) if at is not None and at.tzinfo is None else at


async def read_photo(photo: Optional[UploadFile]) -> Optional[bytes]:
    """The upload as a resized JPEG, or None when there isn't one."""
    if photo is None:
        return None
    data = await photo.read(settings.MAX_PHOTO_BYTES + 1)
    if not data:
        return None
    if len(data) > settings.MAX_PHOTO_BYTES:
        raise error(413, "file_too_large", f"Photos can be up to {settings.MAX_PHOTO_BYTES // (1024 * 1024)} MB")
    if sniff_image(data) is None:
        raise error(400, "validation_error", "Photos must be JPEG or PNG")
    try:
        return await shrink_image(data, PHOTO_SIDE)
    except ImageError:
        raise error(400, "validation_error", "Photos must be JPEG or PNG")


async def add_message(
    db: AsyncSession, ticket: SupportTicket, *, kind: MessageKind, author: Optional[User], body: str,
    photo: Optional[bytes] = None, author_name: Optional[str] = None,
) -> TicketMessage:
    body = (body or "").strip()
    if len(body) > MAX_BODY:
        raise error(400, "validation_error", f"Messages can be up to {MAX_BODY} characters")
    if not body and photo is None and kind != MessageKind.EVENT:
        raise error(400, "validation_error", "Write a message or add a photo")
    message = TicketMessage(
        id=uuid_pkg.uuid4(), ticket_id=ticket.id, kind=kind, author_id=getattr(author, "id", None),
        author_name=author_name if author_name is not None else person_name(author), body=body, channel=ticket.channel,
        created_at=now(),
    )
    if photo is not None:
        key = f"support/{ticket.id}/{message.id}.jpg"
        message.attachment_url = await get_storage().save(key, photo, "image/jpeg")
        message.attachment_key = key
    db.add(message)
    if kind in (MessageKind.CUSTOMER, MessageKind.AGENT):
        ticket.last_message_at = message.created_at
    if kind == MessageKind.CUSTOMER:
        ticket.last_customer_message_at = message.created_at
    return message


def event(db: AsyncSession, ticket: SupportTicket, author: Optional[User], text: str) -> None:
    db.add(TicketMessage(ticket_id=ticket.id, kind=MessageKind.EVENT, author_id=getattr(author, "id", None),
                         author_name=person_name(author) if author else "System", body=text, channel=ticket.channel,
                         created_at=now()))


def set_status(ticket: SupportTicket, status: str) -> None:
    ticket.status = status
    ticket.resolved_at = now() if status in (TicketStatus.RESOLVED, TicketStatus.CLOSED) else None


def message_out(m: TicketMessage, *, for_team: bool) -> MessageOut:
    # The person sees the team's replies signed "Trimmy support", not an agent's full name.
    name = m.author_name if for_team or m.kind != MessageKind.AGENT else f"{(m.author_name or 'Trimmy').split(' ')[0]} · Trimmy support"
    return MessageOut(id=str(m.id), kind=m.kind, authorName=name, body=m.body, attachmentUrl=m.attachment_url,
                      channel=m.channel, createdAt=aware(m.created_at))


async def last_messages(db: AsyncSession, tickets: list[SupportTicket], *, for_team: bool) -> dict:
    """ticket id → its latest visible message (one query)."""
    if not tickets:
        return {}
    kinds = [MessageKind.CUSTOMER, MessageKind.AGENT] + ([MessageKind.NOTE] if for_team else [])
    rows = (await db.execute(
        select(TicketMessage).where(TicketMessage.ticket_id.in_([t.id for t in tickets]), TicketMessage.kind.in_(kinds))
        .order_by(TicketMessage.ticket_id, TicketMessage.created_at.desc())
    )).scalars().all()
    latest: dict = {}
    for m in rows:
        latest.setdefault(m.ticket_id, m)
    return latest


def ticket_row(t: SupportTicket, last: Optional[TicketMessage], assignee_name: Optional[str] = None) -> TicketRow:
    preview = ""
    if last is not None:
        preview = (last.body or ("Photo" if last.attachment_url else ""))[:PREVIEW]
    return TicketRow(
        id=str(t.id), number=t.number, subject=t.subject, category=t.category, status=t.status, priority=t.priority,
        channel=t.channel, requesterId=str(t.requester_id) if t.requester_id else None, requesterName=t.requester_name,
        businessId=str(t.business_id) if t.business_id else None, bookingId=str(t.booking_id) if t.booking_id else None,
        assigneeId=str(t.assignee_id) if t.assignee_id else None, assigneeName=assignee_name,
        createdAt=aware(t.created_at), lastMessageAt=aware(t.last_message_at),
        lastCustomerMessageAt=aware(t.last_customer_message_at), firstResponseAt=aware(t.first_response_at), preview=preview, lastFrom=last.kind if last is not None else None,
    )


async def notify_reply(db: AsyncSession, ticket: SupportTicket, message: TicketMessage) -> None:
    """Tell the person the team answered: inbox, live stream and push (their settings allow)."""
    if not ticket.requester_id:
        return
    from app.modules.notifications.dispatch import deliver

    preview = (message.body or "Sent you a photo")[:PREVIEW]
    await deliver(
        db, [ticket.requester_id], type="support_reply", title=f"Trimmy support replied (#{ticket.number})",
        body=preview, data={"ticketId": str(ticket.id), "number": ticket.number}, event_key=f"support_reply:{message.id}",
    )
