"""Help, for anyone signed in (customers, owners, staff): open a ticket, see the conversation,
write back, add a photo, mark it sorted. Internal notes and the team's own notes never show here."""
import uuid as uuid_pkg
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.rate_limit import limiter
from app.modules.auth.models import User
from app.modules.booking.models import Booking
from app.modules.business.models import Business, Staff
from app.modules.support import service
from app.modules.support.models import MessageKind, SupportTicket, TicketChannel, TicketMessage, TicketStatus
from app.modules.support.schemas import CreateTicketRequest, MyTicketsResponse, TicketResponse

router = APIRouter(prefix="/support", tags=["Support"])

CATEGORY_SUBJECT = {
    "booking": "A booking", "payment": "A payment", "refund": "A refund", "account": "My account",
    "business": "My business", "bug": "Something isn't working", "other": "Something else",
}


def _uuid(value: Optional[str]) -> Optional[uuid_pkg.UUID]:
    if not value:
        return None
    try:
        return uuid_pkg.UUID(value)
    except ValueError:
        raise service.error(404, "not_found", "Not found")


async def _member_of(db: AsyncSession, user: User, business_id) -> bool:
    owner = (await db.execute(select(Business.id).where(Business.id == business_id, Business.owner_id == user.id))).first()
    staff = (await db.execute(select(Staff.id).where(Staff.business_id == business_id, Staff.user_id == user.id))).first()
    return bool(owner or staff)


async def _mine(db: AsyncSession, user: User, ticket_id: str) -> SupportTicket:
    ticket = await db.get(SupportTicket, _uuid(ticket_id))
    if ticket is None or ticket.requester_id != user.id:
        raise service.error(404, "not_found", "No ticket with that id")
    return ticket


async def _response(db: AsyncSession, ticket: SupportTicket) -> TicketResponse:
    messages = (await db.execute(
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket.id, TicketMessage.kind.in_([MessageKind.CUSTOMER, MessageKind.AGENT]))
        .order_by(TicketMessage.created_at)
    )).scalars().all()
    reference = (await db.execute(select(Booking.reference).where(Booking.id == ticket.booking_id))).scalar_one_or_none() if ticket.booking_id else None
    business = (await db.execute(select(Business.name).where(Business.id == ticket.business_id))).scalar_one_or_none() if ticket.business_id else None
    last = next((m for m in reversed(messages)), None)
    return TicketResponse(ticket=service.ticket_row(ticket, last), bookingReference=reference, businessName=business,
                          messages=[service.message_out(m, for_team=False) for m in messages])


@router.post("/tickets", response_model=TicketResponse, status_code=201)
@limiter.limit("10/hour")
async def create_ticket(request: Request, data: CreateTicketRequest, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """A booking must be theirs (as the customer, or at a business they work at); a business,
    one they own or work at. The booking's business is attached too."""
    booking_id, business_id = _uuid(data.bookingId), _uuid(data.businessId)
    if booking_id:
        booking = await db.get(Booking, booking_id)
        if booking is None or (booking.customer_id != user.id and not await _member_of(db, user, booking.business_id)):
            raise service.error(404, "not_found", "No booking with that id")
        business_id = business_id or booking.business_id
    if business_id and not booking_id and not await _member_of(db, user, business_id):
        raise service.error(404, "not_found", "No business with that id")

    ticket = SupportTicket(
        id=uuid_pkg.uuid4(), requester_id=user.id, requester_name=service.person_name(user), requester_phone=user.phone,
        requester_email=user.email, business_id=business_id, booking_id=booking_id, channel=TicketChannel.IN_APP,
        category=data.category, subject=(data.subject or "").strip() or CATEGORY_SUBJECT[data.category],
        status=TicketStatus.NEW,
    )
    db.add(ticket)
    await db.flush()
    await service.add_message(db, ticket, kind=MessageKind.CUSTOMER, author=user, body=data.message)
    await db.commit()
    return await _response(db, ticket)


@router.get("/tickets", response_model=MyTicketsResponse)
async def my_tickets(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Newest activity first (at most 50: older ones are long sorted)."""
    tickets = (await db.execute(
        select(SupportTicket).where(SupportTicket.requester_id == user.id).order_by(SupportTicket.last_message_at.desc()).limit(50)
    )).scalars().all()
    latest = await service.last_messages(db, list(tickets), for_team=False)
    return MyTicketsResponse(tickets=[service.ticket_row(t, latest.get(t.id)) for t in tickets])


@router.get("/tickets/{ticket_id}", response_model=TicketResponse)
async def get_ticket(ticket_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _response(db, await _mine(db, user, ticket_id))


@router.post("/tickets/{ticket_id}/messages", response_model=TicketResponse)
@limiter.limit("30/hour")
async def reply(
    request: Request,
    ticket_id: str,
    body: str = Form(""),
    photo: Optional[UploadFile] = File(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Write back (multipart: body and/or photo). A sorted or closed ticket reopens."""
    ticket = await _mine(db, user, ticket_id)
    await service.add_message(db, ticket, kind=MessageKind.CUSTOMER, author=user, body=body, photo=await service.read_photo(photo))
    if ticket.status != TicketStatus.NEW:
        if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            service.event(db, ticket, user, "Reopened by their reply")
        service.set_status(ticket, TicketStatus.OPEN)
    await db.commit()
    return await _response(db, ticket)


@router.post("/tickets/{ticket_id}/resolve", response_model=TicketResponse)
async def mark_sorted(ticket_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The person says it's sorted."""
    ticket = await _mine(db, user, ticket_id)
    if ticket.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        service.set_status(ticket, TicketStatus.RESOLVED)
        service.event(db, ticket, user, "Marked sorted by them")
        await db.commit()
    return await _response(db, ticket)
