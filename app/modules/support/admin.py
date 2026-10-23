"""The team's side of support (/admin/support/*): the inbox, a ticket with everything about the
person beside it, replies and internal notes, assigning, statuses, canned replies.

Ticket changes are recorded on the ticket itself (event messages: "Assigned to Amani"), not in
the platform audit log, which stays for actions on accounts, businesses and money."""
import uuid as uuid_pkg
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_admin
from app.modules.admin.lookup import booking_rows, business_rows, user_row
from app.modules.auth.models import User
from app.modules.booking.models import Booking
from app.modules.business.models import Business
from app.modules.support import service
from app.modules.support.models import MessageKind, SupportMacro, SupportTicket, TicketMessage, TicketStatus
from app.modules.support.schemas import (
    AdminTicketDetail, AdminTicketPage, MacroIn, MacroOut, MacrosResponse, SupportSummary, UpdateTicketRequest,
)

router = APIRouter(prefix="/admin/support", tags=["Admin"])

PAGE = 40
ACTIVE = (TicketStatus.NEW, TicketStatus.OPEN)
STATUS_LABEL = {"new": "New", "open": "Open", "waiting": "Waiting on them", "resolved": "Resolved", "closed": "Closed"}


def _uuid(value: str, what: str = "ticket") -> uuid_pkg.UUID:
    try:
        return uuid_pkg.UUID(str(value))
    except ValueError:
        raise service.error(404, "not_found", f"No {what} with that id")


async def _ticket(db: AsyncSession, ticket_id: str) -> SupportTicket:
    ticket = await db.get(SupportTicket, _uuid(ticket_id))
    if ticket is None:
        raise service.error(404, "not_found", "No ticket with that id")
    return ticket


async def _assignee_names(db: AsyncSession, tickets) -> dict:
    ids = {t.assignee_id for t in tickets if t.assignee_id}
    if not ids:
        return {}
    return {u.id: service.person_name(u) for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()}


@router.get("/summary", response_model=SupportSummary)
async def summary(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Counts for the inbox tabs and the nav badge."""
    active = SupportTicket.status.in_(ACTIVE)
    row = (await db.execute(select(
        func.count().filter(and_(active, SupportTicket.assignee_id.is_(None))),
        func.count().filter(and_(active, SupportTicket.assignee_id == admin.id)),
        func.count().filter(active),
        func.count().filter(SupportTicket.status == TicketStatus.WAITING),
        func.min(SupportTicket.last_customer_message_at).filter(active),
    ).select_from(SupportTicket))).one()
    oldest = datetime.fromisoformat(row[4]) if isinstance(row[4], str) else row[4]  # SQLite: text
    return SupportSummary(unassigned=row[0], mine=row[1], open=row[2], waiting=row[3], oldestWaitingSince=service.aware(oldest))


@router.get("/tickets", response_model=AdminTicketPage)
async def list_tickets(
    view: Literal["unassigned", "mine", "all"] = "all",
    status: Literal["active", "waiting", "resolved", "closed", "all"] = "active",
    q: Optional[str] = None,
    cursor: Optional[str] = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Latest activity first. 'active' = new or open (needs the team)."""
    query = select(SupportTicket)
    if view == "unassigned":
        query = query.where(SupportTicket.assignee_id.is_(None))
    elif view == "mine":
        query = query.where(SupportTicket.assignee_id == admin.id)
    if status == "active":
        query = query.where(SupportTicket.status.in_(ACTIVE))
    elif status != "all":
        query = query.where(SupportTicket.status == status)
    if q and q.strip():
        text = q.strip().lstrip("#").lower()
        digits = "".join(ch for ch in text if ch.isdigit())
        terms = [func.lower(SupportTicket.number) == text, func.lower(SupportTicket.requester_name).contains(text),
                 func.lower(SupportTicket.requester_email).contains(text), func.lower(SupportTicket.subject).contains(text)]
        if len(digits) >= 6:
            terms.append(SupportTicket.requester_phone.contains(digits.removeprefix("254").lstrip("0")))
        query = query.where(or_(*terms))
    if cursor:
        try:
            raw_at, raw_id = cursor.split("|", 1)
            at, cid = datetime.fromisoformat(raw_at), uuid_pkg.UUID(raw_id)
        except ValueError:
            raise service.error(400, "validation_error", "Bad cursor")
        query = query.where(or_(SupportTicket.last_message_at < at, and_(SupportTicket.last_message_at == at, SupportTicket.id < cid)))
    rows = (await db.execute(query.order_by(SupportTicket.last_message_at.desc(), SupportTicket.id.desc()).limit(PAGE + 1))).scalars().all()
    page = list(rows[:PAGE])
    latest = await service.last_messages(db, page, for_team=True)
    names = await _assignee_names(db, page)
    return AdminTicketPage(
        tickets=[service.ticket_row(t, latest.get(t.id), names.get(t.assignee_id)) for t in page],
        nextCursor=f"{page[-1].last_message_at.isoformat()}|{page[-1].id}" if len(rows) > PAGE else None,
    )


async def _detail(db: AsyncSession, ticket: SupportTicket) -> AdminTicketDetail:
    messages = (await db.execute(
        select(TicketMessage).where(TicketMessage.ticket_id == ticket.id).order_by(TicketMessage.created_at)
    )).scalars().all()
    names = await _assignee_names(db, [ticket])
    last = next((m for m in reversed(messages) if m.kind != MessageKind.EVENT), None)

    requester = await db.get(User, ticket.requester_id) if ticket.requester_id else None
    booking = await db.get(Booking, ticket.booking_id) if ticket.booking_id else None
    business = await db.get(Business, ticket.business_id) if ticket.business_id else None
    recent = []
    others = []
    if requester is not None:
        recent = (await db.execute(
            select(Booking).where(Booking.customer_id == requester.id).order_by(Booking.created_at.desc()).limit(5)
        )).scalars().all()
        others = (await db.execute(
            select(SupportTicket).where(SupportTicket.requester_id == requester.id, SupportTicket.id != ticket.id)
            .order_by(SupportTicket.last_message_at.desc()).limit(5)
        )).scalars().all()
    return AdminTicketDetail(
        ticket=service.ticket_row(ticket, last, names.get(ticket.assignee_id)),
        messages=[service.message_out(m, for_team=True) for m in messages],
        requester=user_row(requester) if requester else None,
        booking=(await booking_rows(db, [booking]))[0] if booking else None,
        business=(await business_rows(db, [business]))[0] if business else None,
        recentBookings=await booking_rows(db, recent),
        otherTickets=[service.ticket_row(t, None) for t in others],
    )


@router.get("/tickets/{ticket_id}", response_model=AdminTicketDetail)
async def get_ticket(ticket_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await _detail(db, await _ticket(db, ticket_id))


@router.post("/tickets/{ticket_id}/messages", response_model=AdminTicketDetail)
async def reply(
    ticket_id: str,
    body: str = Form(""),
    internal: bool = Form(False),
    status: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """A reply (the person is notified) or an internal note (team only). A reply moves the ticket
    to 'waiting' unless `status` says otherwise (e.g. reply and resolve); taking an unassigned
    ticket's first reply assigns it to you."""
    ticket = await _ticket(db, ticket_id)
    if status is not None and status not in STATUS_LABEL:
        raise service.error(400, "validation_error", "Unknown status")
    kind = MessageKind.NOTE if internal else MessageKind.AGENT
    photo_data = await service.read_photo(photo)
    if not internal and ticket.assignee_id is None and (body.strip() or photo_data):
        ticket.assignee_id = admin.id
        service.event(db, ticket, admin, f"Assigned to {service.person_name(admin)}")
    message = await service.add_message(db, ticket, kind=kind, author=admin, body=body, photo=photo_data)
    if not internal:
        ticket.first_response_at = ticket.first_response_at or message.created_at
        new_status = status or TicketStatus.WAITING
        if new_status != ticket.status:
            service.set_status(ticket, new_status)
        await db.commit()
        await service.notify_reply(db, ticket, message)
    else:
        if status and status != ticket.status:
            service.set_status(ticket, status)
            service.event(db, ticket, admin, f"Status: {STATUS_LABEL[status]}")
        await db.commit()
    return await _detail(db, ticket)


@router.patch("/tickets/{ticket_id}", response_model=AdminTicketDetail)
async def update_ticket(ticket_id: str, data: UpdateTicketRequest, admin: User = Depends(require_admin),
                        db: AsyncSession = Depends(get_db)):
    ticket = await _ticket(db, ticket_id)
    if data.status and data.status != ticket.status:
        service.set_status(ticket, data.status)
        service.event(db, ticket, admin, f"Status: {STATUS_LABEL[data.status]}")
    if data.priority and data.priority != ticket.priority:
        ticket.priority = data.priority
        service.event(db, ticket, admin, f"Priority: {data.priority}")
    if data.category and data.category != ticket.category:
        ticket.category = data.category
        service.event(db, ticket, admin, f"Category: {data.category}")
    if data.assigneeId is not None:
        if data.assigneeId == "":
            if ticket.assignee_id is not None:
                ticket.assignee_id = None
                service.event(db, ticket, admin, "Unassigned")
        else:
            who = admin if data.assigneeId == "me" else await db.get(User, _uuid(data.assigneeId, "admin"))
            if who is None or who.platform_role != "admin":
                raise service.error(400, "validation_error", "Tickets can only be assigned to admins")
            if ticket.assignee_id != who.id:
                ticket.assignee_id = who.id
                service.event(db, ticket, admin, f"Assigned to {service.person_name(who)}")
    if data.bookingId is not None:
        booking = await db.get(Booking, _uuid(data.bookingId, "booking")) if data.bookingId else None
        if data.bookingId and booking is None:
            raise service.error(404, "not_found", "No booking with that id")
        ticket.booking_id = booking.id if booking else None
        if booking is not None:
            ticket.business_id = booking.business_id
            service.event(db, ticket, admin, f"Linked booking {booking.reference}")
    if data.businessId is not None:
        business = await db.get(Business, _uuid(data.businessId, "business")) if data.businessId else None
        if data.businessId and business is None:
            raise service.error(404, "not_found", "No business with that id")
        ticket.business_id = business.id if business else None
        if business is not None:
            service.event(db, ticket, admin, f"Linked business {business.name}")
    await db.commit()
    return await _detail(db, ticket)


# ─── Canned replies ──────────────────────────────────────────


def _macro(m: SupportMacro) -> MacroOut:
    return MacroOut(id=str(m.id), title=m.title, body=m.body, updatedAt=m.updated_at)


@router.get("/macros", response_model=MacrosResponse)
async def list_macros(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SupportMacro).order_by(SupportMacro.title))).scalars().all()
    return MacrosResponse(macros=[_macro(m) for m in rows])


@router.post("/macros", response_model=MacroOut, status_code=201)
async def create_macro(data: MacroIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    macro = SupportMacro(title=data.title.strip(), body=data.body.strip(), created_by=admin.id)
    db.add(macro)
    await db.commit()
    return _macro(macro)


@router.put("/macros/{macro_id}", response_model=MacroOut)
async def update_macro(macro_id: str, data: MacroIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    macro = await db.get(SupportMacro, _uuid(macro_id, "canned reply"))
    if macro is None:
        raise service.error(404, "not_found", "No canned reply with that id")
    macro.title, macro.body = data.title.strip(), data.body.strip()
    await db.commit()
    return _macro(macro)


@router.delete("/macros/{macro_id}", status_code=204)
async def delete_macro(macro_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    macro = await db.get(SupportMacro, _uuid(macro_id, "canned reply"))
    if macro is not None:
        await db.delete(macro)
        await db.commit()
