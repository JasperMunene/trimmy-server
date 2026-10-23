"""What each booking notification says: the in-app/push title and body, plus the SMS and email
versions where a kind has them (booking/tasks.py decides who gets what; dispatch.py and each
person's settings decide the channels).

Emails are plain HTML with inline styles and no images, so they render the same in Gmail, Outlook
and phone mail apps and load on a slow connection.
"""
import html
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


@dataclass
class BookingContext:
    booking: object
    business_name: str
    staff_name: Optional[str]
    starts: datetime  # in the business's timezone
    address: Optional[str]
    map_link: Optional[str]
    lines: list[tuple[str, int, int]]  # (name, quantity, line total)
    payment_method: Optional[str] = None
    payment_reference: Optional[str] = None

    @property
    def when(self) -> str:
        return self.starts.strftime("%a %-d %b, %H:%M")

    @property
    def what(self) -> str:
        names = [name for name, _, _ in self.lines]
        if not names:
            return "your appointment"
        return names[0] if len(names) == 1 else f"{names[0]} +{len(names) - 1} more"

    @property
    def ref(self) -> str:
        return self.booking.reference

    @property
    def cur(self) -> str:
        return self.booking.currency

    @property
    def customer(self) -> str:
        return self.booking.customer_name or "A customer"


async def load_context(db: AsyncSession, booking_id) -> Optional[BookingContext]:
    from app.modules.booking.models import Booking
    from app.modules.business.models import Business, Staff

    booking = (await db.execute(
        select(Booking).where(Booking.id == booking_id).options(selectinload(Booking.lines))
    )).scalar_one_or_none()
    if booking is None:
        return None
    business = await db.get(Business, booking.business_id)
    staff = await db.get(Staff, booking.staff_id) if booking.staff_id else None
    tz = ZoneInfo(business.timezone if business else "Africa/Nairobi")
    starts = booking.starts_at if booking.starts_at.tzinfo else booking.starts_at.replace(tzinfo=timezone.utc)
    location = (business.location if business else None) or {}
    map_link = (f"https://maps.google.com/?q={location['lat']},{location['lng']}"
                if location.get("lat") is not None and location.get("lng") is not None else None)
    from app.modules.payments.allocations import payment_parts

    # Including a visit checkout that covered this booking along with others.
    parts = await payment_parts(db, [booking.id], succeeded_only=True)
    settled = max((part.payment for part in parts), key=lambda p: p.settled_at or p.created_at, default=None)
    method = reference = None
    if booking.payment_status == "paid":
        method, reference = ("M-Pesa", settled.mpesa_receipt) if settled is not None and settled.kind == "charge" else ("Cash", None)
    return BookingContext(
        booking=booking,
        business_name=business.name if business else "your salon",
        staff_name=staff.name if staff else None,
        starts=starts.astimezone(tz),
        address=location.get("address"),
        map_link=map_link,
        lines=[(line.name, line.quantity, line.price_amount * line.quantity) for line in booking.lines],
        payment_method=method,
        payment_reference=reference,
    )


@dataclass
class Message:
    title: str
    body: str
    sms: Optional[str] = None
    email: Optional[dict] = None  # {"subject", "html"}
    extra_rows: list = field(default_factory=list)


def _money(cur: str, amount: int) -> str:
    return f"{cur} {amount:,}"


# ─── Email layout ────────────────────────────────────────────


def booking_email(ctx: BookingContext, heading: str, intro: str, *, rows: Optional[list[tuple[str, str]]] = None,
                  notes: Optional[list[str]] = None, show_lines: bool = True) -> str:
    e = html.escape
    details = [("Reference", ctx.ref), ("Where", ctx.business_name + (f", {ctx.address}" if ctx.address else "")),
               ("When", ctx.when)]
    if ctx.staff_name:
        details.append(("With", ctx.staff_name))
    details += rows or []
    cell = 'style="padding:6px 0;vertical-align:top;color:#6A6A6A;width:110px"'
    value = 'style="padding:6px 0;vertical-align:top;color:#222"'
    table = "".join(f"<tr><td {cell}>{e(k)}</td><td {value}>{e(v)}</td></tr>" for k, v in details)
    services = ""
    if show_lines and ctx.lines:
        items = "".join(
            f'<tr><td {value}>{e(name)}{f" × {qty}" if qty > 1 else ""}</td>'
            f'<td style="padding:6px 0;text-align:right;color:#222">{e(_money(ctx.cur, total))}</td></tr>'
            for name, qty, total in ctx.lines
        )
        services = (
            '<table role="presentation" width="100%" style="border-collapse:collapse;margin-top:12px;'
            'border-top:1px solid #EBEBEB">' + items +
            f'<tr><td style="padding:8px 0;font-weight:bold;color:#222">Total</td>'
            f'<td style="padding:8px 0;text-align:right;font-weight:bold;color:#222">'
            f'{e(_money(ctx.cur, ctx.booking.total_amount))}</td></tr></table>'
        )
    button = ""
    if ctx.map_link:
        button = (f'<p style="margin:20px 0"><a href="{e(ctx.map_link, quote=True)}" style="display:inline-block;'
                  'padding:12px 20px;background:#222;color:#fff;border-radius:999px;text-decoration:none">'
                  'Get directions</a></p>')
    paragraphs = "".join(f'<p style="margin:12px 0;color:#222">{e(p)}</p>' for p in notes or [])
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;'
        'margin:0 auto;padding:16px;font-size:15px;line-height:1.5">'
        f'<h2 style="margin:0 0 8px;color:#222">{e(heading)}</h2>'
        f'<p style="margin:0 0 12px;color:#222">{e(intro)}</p>'
        f'<table role="presentation" width="100%" style="border-collapse:collapse">{table}</table>'
        f"{services}{paragraphs}{button}"
        '<p style="margin:24px 0 0;color:#9A9A9A;font-size:13px">You can choose which emails you get in the '
        "Trimmy app: Notifications → Settings.</p></div>"
    )


# ─── To the customer ─────────────────────────────────────────


def customer_message(kind: str, ctx: BookingContext, extra: dict) -> Message:
    b, name, cur = ctx.booking, ctx.business_name, ctx.cur
    refund = b.refund_amount or 0
    refund_line = f"{name} will refund your {_money(cur, refund)} deposit." if refund and b.refund_status == "due" else None

    if kind == "confirmed":
        rows = []
        if b.payment_status == "deposit_paid" and b.deposit_amount:
            rows.append(("Deposit paid", _money(cur, b.deposit_amount)))
            rows.append(("To pay there", _money(cur, max(0, b.total_amount - b.deposit_amount))))
        return Message(
            "Booking confirmed", f"{name}: {ctx.what}, {ctx.when}. Ref {ctx.ref}.",
            email={"subject": f"Booking confirmed: {name}, {ctx.when}",
                   "html": booking_email(ctx, "You're booked", f"{name} is expecting you.", rows=rows,
                                         notes=["Need to change it? Open the Trimmy app to reschedule or cancel."])},
        )
    if kind == "cancelled_by_business":
        notes = [refund_line] if refund_line else []
        return Message(
            "Booking cancelled", f"{name} cancelled your {ctx.what} on {ctx.when}." + (f" {refund_line}" if refund_line else ""),
            sms=f"Trimmy: {name} cancelled your {ctx.what} on {ctx.when} (ref {ctx.ref})." + (f" {refund_line}" if refund_line else ""),
            email={"subject": f"Cancelled: {name}, {ctx.when}",
                   "html": booking_email(ctx, "Your booking was cancelled", f"{name} cancelled this booking.",
                                         notes=notes + ["You can book another time in the Trimmy app."])},
        )
    if kind == "rescheduled_by_business":
        old = extra.get("old_when")
        moved = f" from {old}" if old else ""
        return Message(
            "Booking moved", f"{name} moved your {ctx.what}{moved} to {ctx.when}.",
            sms=f"Trimmy: {name} moved your {ctx.what}{moved} to {ctx.when} (ref {ctx.ref}). Open the app to change it.",
            email={"subject": f"New time: {name}, {ctx.when}",
                   "html": booking_email(ctx, "Your booking has a new time", f"{name} moved your booking{moved}.",
                                         notes=["If the new time doesn't work, you can change or cancel it in the Trimmy app."])},
        )
    if kind == "payment_failed":
        until = None
        if b.expires_at is not None:
            expires = b.expires_at if b.expires_at.tzinfo else b.expires_at.replace(tzinfo=timezone.utc)
            until = expires.astimezone(ctx.starts.tzinfo).strftime("%H:%M")
        return Message("Payment didn't go through",
                       f"Your deposit for {ctx.what} at {name} wasn't paid." + (f" Try again before {until}." if until else ""))
    if kind == "hold_expired":
        return Message("Booking not completed",
                       f"The payment window for {ctx.what} at {name} on {ctx.when} closed, so the time was released.")
    if kind == "refund_due":
        amount = _money(cur, int(extra.get("amount") or refund))
        why = extra.get("why") or "This booking"
        text = f"{why}. {name} will refund you {amount}."
        return Message(
            "Refund on the way", text, sms=f"Trimmy: {text} Ref {ctx.ref}.",
            email={"subject": f"Refund due from {name}",
                   "html": booking_email(ctx, "You're owed a refund", text, rows=[("Refund", amount)], show_lines=False)},
        )
    if kind == "refunded":
        reference = f" (ref {b.refund_reference})" if b.refund_reference else ""
        text = f"{name} refunded {_money(cur, refund)} for booking {ctx.ref}{reference}."
        rows = [("Refunded", _money(cur, refund))] + ([("Refund ref", b.refund_reference)] if b.refund_reference else [])
        return Message(
            "Refund sent", text, sms=f"Trimmy: {text}",
            email={"subject": f"Refund from {name}",
                   "html": booking_email(ctx, "Your refund was sent", text, rows=rows, show_lines=False)},
        )
    if kind == "no_show":
        fee = b.no_show_fee or 0
        text = f"You were marked as not attending {ctx.what} at {name} on {ctx.when}."
        rows = []
        if fee:
            text += f" {_money(cur, fee)} of your deposit was kept as a no-show fee."
            rows.append(("No-show fee", _money(cur, fee)))
        if refund:
            text += f" {_money(cur, refund)} will be refunded."
            rows.append(("Refund", _money(cur, refund)))
        return Message(
            "Missed appointment", text,
            email={"subject": f"Missed appointment at {name}",
                   "html": booking_email(ctx, "We missed you", text, rows=rows, show_lines=False,
                                         notes=["If this is wrong, contact the business directly."])},
        )
    if kind == "completed":
        rows = []
        if ctx.payment_method:
            rows.append(("Paid", f"{_money(cur, b.total_amount)} by {ctx.payment_method}"))
        if ctx.payment_reference:
            rows.append(("M-Pesa ref", ctx.payment_reference))
        return Message(
            f"Thanks for visiting {name}", f"How was your {ctx.what}? Leave a review in the app.",
            email={"subject": f"Your receipt from {name}",
                   "html": booking_email(ctx, "Thanks for your visit", f"Here's your receipt from {name}.", rows=rows,
                                         notes=["Enjoyed it? Leave a review in the Trimmy app."])},
        )
    if kind == "reminder_24h":
        return Message(
            "See you tomorrow", f"{ctx.what} at {name}, {ctx.when}.",
            email={"subject": f"Reminder: {name}, {ctx.when}",
                   "html": booking_email(ctx, "See you tomorrow", f"A reminder of your booking at {name}.",
                                         notes=["Need to change it? Open the Trimmy app."])},
        )
    if kind == "reminder_2h":
        return Message("Coming up soon", f"{ctx.what} at {name} at {ctx.starts.strftime('%H:%M')}.")
    raise ValueError(f"Not a customer kind: {kind}")


# ─── To the business (owner, front desk, staff) ──────────────


def business_message(kind: str, ctx: BookingContext, extra: dict) -> Message:
    b, cur, what, who = ctx.booking, ctx.cur, ctx.what, ctx.customer
    if kind == "created":
        return Message("New booking", f"{who} booked {what}, {ctx.when}" + (f" with {ctx.staff_name}." if ctx.staff_name else "."))
    if kind == "cancelled_by_customer":
        owed = f" Refund owed: {_money(cur, b.refund_amount)}." if b.refund_amount and b.refund_status == "due" else ""
        return Message("Booking cancelled", f"{who} cancelled {what} on {ctx.when}.{owed}")
    if kind == "rescheduled_by_customer":
        old = extra.get("old_when")
        return Message("Booking moved", f"{who} moved {what}" + (f" from {old}" if old else "") + f" to {ctx.when}.")
    if kind == "assigned":
        return Message("New booking", f"{what} with {who}, {ctx.when}.")
    if kind == "cancelled_for_staff":
        return Message("Booking cancelled", f"{what} with {who} on {ctx.when} was cancelled.")
    if kind == "refund_owed":
        amount = _money(cur, int(extra.get("amount") or b.refund_amount or 0))
        return Message("Refund owed", f"{who} is owed {amount} for {ctx.ref}. {extra.get('why') or ''}".strip())
    raise ValueError(f"Not a business kind: {kind}")
