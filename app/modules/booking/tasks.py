import asyncio
import logging
import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.workers.registry import PermanentError, periodic, task

logger = logging.getLogger(__name__)


@periodic("booking.expire_holds", every=60)
async def expire_holds_job(db: AsyncSession) -> None:
    """Release unpaid bookings whose 10-minute hold ran out (D6, BK-24).

    Reads and new bookings also do this for the business or customer they
    touch; this catches everything else within a minute. Safe to run on
    several workers at once (rows are locked with SKIP LOCKED on Postgres).
    """
    from app.modules.booking.bookings import expire_holds

    released = await expire_holds(db, datetime.now(timezone.utc))
    await db.commit()
    if released:
        logger.info(f"Released {released} expired booking hold(s)")


def confirmation_sms(
    reference: str, business: str, services: list[str], staff: Optional[str], when: datetime, deposit: Optional[int], currency: str
) -> str:
    """One SMS segment where possible, e.g.
    "Trimmy: BK7F3AQ9 confirmed. Glow Beauty, Classic Haircut with Wanjiku, Mon 5 Oct 14:30. Deposit KES 300 paid."
    """
    if not services:
        what = "your appointment"
    elif len(services) == 1:
        what = services[0]
    else:
        what = f"{services[0]} +{len(services) - 1} more"
    who = f" with {staff}" if staff else ""
    paid = f" Deposit {currency} {deposit:,} paid." if deposit else ""
    return f"Trimmy: {reference} confirmed. {business}, {what}{who}, {when.strftime('%a %-d %b %H:%M')}.{paid}"


@task("booking.send_confirmation", queue="sms", max_retries=5, inline_fallback=True)
async def send_booking_confirmation(booking_id: str, db: AsyncSession) -> None:
    """Text the customer that their booking is confirmed (BK-40). Sent at most once per booking."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from zoneinfo import ZoneInfo

    from app.modules.auth.sms import deliver_sms
    from app.modules.booking.models import Booking, BookingStatus
    from app.modules.business.models import Business, Staff

    try:
        bid = uuid_pkg.UUID(booking_id)
    except ValueError:
        raise PermanentError(f"Bad booking id {booking_id!r}")

    # Claim it first: whoever flips confirmation_sent_at from NULL sends, so retries and
    # concurrent workers can't double-text.
    claimed = await db.execute(
        update(Booking)
        .where(Booking.id == bid, Booking.confirmation_sent_at.is_(None), Booking.status == BookingStatus.CONFIRMED,
               Booking.customer_phone.is_not(None))
        .values(confirmation_sent_at=datetime.now(timezone.utc))
    )
    await db.commit()
    if claimed.rowcount == 0:
        return  # already sent, no longer confirmed, or no phone to text
    if not settings.NOTIFY_SMS_ENABLED:
        return

    booking = (await db.execute(select(Booking).where(Booking.id == bid).options(selectinload(Booking.lines)))).scalar_one()
    if booking.customer_id is not None:
        from app.modules.notifications.preferences import prefs_for
        if not (await prefs_for(db, [booking.customer_id]))[booking.customer_id].allows("booking_confirmed", "sms"):
            return  # they turned off booking texts
    business = await db.get(Business, booking.business_id)
    staff = await db.get(Staff, booking.staff_id) if booking.staff_id else None
    starts = booking.starts_at if booking.starts_at.tzinfo else booking.starts_at.replace(tzinfo=timezone.utc)
    message = confirmation_sms(
        booking.reference, business.name if business else "your salon", [line.name for line in booking.lines],
        staff.name if staff else None, starts.astimezone(ZoneInfo(business.timezone if business else "Africa/Nairobi")),
        booking.deposit_amount if booking.payment_status == "deposit_paid" else None, booking.currency,
    )
    try:
        # The SMS gateway call is a blocking HTTP request; keep it off the event loop.
        await asyncio.to_thread(deliver_sms, booking.customer_phone, message, f"booking {booking.reference} confirmed")
    except Exception:
        # Release the claim so the retry can send it.
        await db.execute(update(Booking).where(Booking.id == bid).values(confirmation_sent_at=None))
        await db.commit()
        raise


async def notify_confirmed(booking_id: uuid_pkg.UUID) -> None:
    """Queue the confirmation SMS and push; call after the commit that confirmed the booking."""
    try:
        await send_booking_confirmation.enqueue(booking_id=str(booking_id))
    except Exception as e:  # never fail the request over a notification
        logger.error(f"Could not queue confirmation for booking {booking_id}: {e}")
    await push_update(booking_id, "confirmed")


# ─── Reminders (BK-62) ───────────────────────────────────────


DAY_BEFORE, TWO_HOURS = timedelta(hours=24), timedelta(hours=2)
REMINDER_SCAN_BATCH = 500


def due_reminder(
    starts: datetime, created: datetime, now: datetime, sent_24h: bool, sent_2h: bool
) -> Optional[str]:
    """Which reminder, if any, a confirmed booking should get now: "24h", "2h" or None.

    - 24h: once the start is a day away or less, but only while more than 2h remain
      (after that the 2h one takes over) and only if it was booked at least a day ahead
      (booked the same day, the confirmation SMS is enough).
    - 2h: once the start is 2h away or less, if it wasn't booked within those 2 hours.
    """
    left = starts - now
    if left <= timedelta(0):
        return None
    if left <= TWO_HOURS:
        return "2h" if not sent_2h and created <= starts - TWO_HOURS else None
    if left <= DAY_BEFORE:
        return "24h" if not sent_24h and created <= starts - DAY_BEFORE else None
    return None


def _what(services: list[str]) -> str:
    if not services:
        return "your appointment"
    return services[0] if len(services) == 1 else f"{services[0]} +{len(services) - 1} more"


def reminder_sms(
    kind: str, *, reference: str, business: str, services: list[str], staff: Optional[str], when: datetime,
    now: datetime, location: Optional[dict] = None,
) -> str:
    """e.g.
    24h: "Trimmy reminder: Glow Beauty, Classic Haircut with Wanjiku, tomorrow 14:30. Ref BK7F3AQ9. To change it, open the Trimmy app."
    2h:  "Trimmy: see you at 14:30 at Glow Beauty (Classic Haircut). Ref BK7F3AQ9. Directions: https://maps.google.com/?q=-1.29,36.78"
    """
    hhmm = when.strftime("%H:%M")
    if kind == "24h":
        today = now.astimezone(when.tzinfo).date()
        day = "tomorrow" if when.date() == today + timedelta(days=1) else ("today" if when.date() == today else when.strftime("%a %-d %b"))
        who = f" with {staff}" if staff else ""
        return f"Trimmy reminder: {business}, {_what(services)}{who}, {day} {hhmm}. Ref {reference}. To change it, open the Trimmy app."
    where = ""
    if location and location.get("lat") is not None and location.get("lng") is not None:
        where = f" Directions: https://maps.google.com/?q={location['lat']},{location['lng']}"
    elif location and location.get("address"):
        where = f" {location['address']}."
    return f"Trimmy: see you at {hhmm} at {business} ({_what(services)}). Ref {reference}.{where}"


@periodic("booking.scan_reminders", every=300)
async def scan_reminders_job(db: AsyncSession) -> None:
    """Every 5 minutes: queue the reminders that are due. Sending (and the once-only
    claim) happens in booking.send_reminder, so a re-queued one is a no-op."""
    from sqlalchemy import or_, select

    from app.modules.booking.models import Booking, BookingStatus

    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Booking.id, Booking.starts_at, Booking.created_at, Booking.reminder_24h_sent_at, Booking.reminder_2h_sent_at)
            .where(
                Booking.status == BookingStatus.CONFIRMED,
                # Someone to remind: a phone to text, or an account (in-app, push, email).
                or_(Booking.customer_phone.is_not(None), Booking.customer_id.is_not(None)),
                Booking.starts_at > now,
                Booking.starts_at <= now + DAY_BEFORE,
                or_(Booking.reminder_24h_sent_at.is_(None), Booking.reminder_2h_sent_at.is_(None)),
            )
            .order_by(Booking.starts_at)
            .limit(REMINDER_SCAN_BATCH)
        )
    ).all()
    queued = 0
    for booking_id, starts, created, sent_24h, sent_2h in rows:
        aware = lambda dt: dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # noqa: E731
        kind = due_reminder(aware(starts), aware(created), now, sent_24h is not None, sent_2h is not None)
        if kind:
            await send_booking_reminder.enqueue(booking_id=str(booking_id), kind=kind)
            queued += 1
    if queued:
        logger.info(f"Queued {queued} booking reminder(s)")


@task("booking.send_reminder", queue="sms", max_retries=3)
async def send_booking_reminder(booking_id: str, kind: str, db: AsyncSession) -> None:
    """Text one reminder, at most once per booking and window."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from zoneinfo import ZoneInfo

    from app.modules.auth.sms import deliver_sms
    from app.modules.booking.models import Booking, BookingStatus
    from app.modules.business.models import Business, Staff
    from app.modules.notifications.preferences import reminder_prefs

    if kind not in ("24h", "2h"):
        raise PermanentError(f"Unknown reminder kind {kind!r}")
    try:
        bid = uuid_pkg.UUID(booking_id)
    except ValueError:
        raise PermanentError(f"Bad booking id {booking_id!r}")
    column = Booking.reminder_24h_sent_at if kind == "24h" else Booking.reminder_2h_sent_at
    now = datetime.now(timezone.utc)

    # Claim it: only the run that flips the column from NULL sends. Also re-checks that the
    # booking is still confirmed and upcoming (it may have been cancelled or moved since).
    claimed = await db.execute(
        update(Booking)
        .where(Booking.id == bid, column.is_(None), Booking.status == BookingStatus.CONFIRMED,
               or_(Booking.customer_phone.is_not(None), Booking.customer_id.is_not(None)), Booking.starts_at > now)
        .values({column.key: now})
    )
    await db.commit()
    if claimed.rowcount == 0:
        return

    booking = (await db.execute(select(Booking).where(Booking.id == bid).options(selectinload(Booking.lines)))).scalar_one()
    # The customer's settings (claimed either way, so a reminder they turned off isn't retried).
    prefs = await reminder_prefs(db, booking.customer_id)
    if not prefs.wants(kind):
        return
    if not (prefs.sms and booking.customer_phone):
        # In the app (and push / email, as their settings say) only.
        await push_update(bid, f"reminder_{kind}")
        return
    business = await db.get(Business, booking.business_id)
    staff = await db.get(Staff, booking.staff_id) if booking.staff_id else None
    tz = ZoneInfo(business.timezone if business else "Africa/Nairobi")
    starts = booking.starts_at if booking.starts_at.tzinfo else booking.starts_at.replace(tzinfo=timezone.utc)
    message = reminder_sms(
        kind, reference=booking.reference, business=business.name if business else "your salon",
        services=[line.name for line in booking.lines], staff=staff.name if staff else None,
        when=starts.astimezone(tz), now=now, location=business.location if business else None,
    )
    try:
        await asyncio.to_thread(deliver_sms, booking.customer_phone, message, f"booking {booking.reference} {kind} reminder")
    except Exception:
        await db.execute(update(Booking).where(Booking.id == bid).values({column.key: None}))
        await db.commit()
        raise
    await push_update(bid, f"reminder_{kind}")  # the inbox, plus push / email per their settings


# ─── Notifications about a booking (BK-41) ───────────────────
# Inbox + live stream always; push / SMS / email per channel switches and each person's settings
# (notifications/dispatch.py). What each kind says is in booking/messages.py.

CUSTOMER_KINDS = (
    "confirmed", "cancelled_by_business", "rescheduled_by_business", "payment_failed", "hold_expired",
    "refund_due", "refunded", "no_show", "completed", "reminder_24h", "reminder_2h",
)
STAFF_KINDS = ("assigned", "cancelled_for_staff")  # the assigned staff member
DESK_KINDS = ("created", "cancelled_by_customer", "rescheduled_by_customer", "refund_owed")  # owner + front desk
PUSH_KINDS = CUSTOMER_KINDS + STAFF_KINDS + DESK_KINDS
# Things that happen to a booking once: a deterministic key, so two code paths reporting the same
# event (e.g. completed by the status stepper and by the charge) notify once.
ONCE_KINDS = ("hold_expired", "refunded", "no_show", "completed")
# Customers without an account (walk-ins, on-behalf bookings) can still get these by SMS/email.
GUEST_KINDS = ("confirmed", "cancelled_by_business", "rescheduled_by_business", "refund_due", "refunded", "no_show", "completed")


async def desk_user_ids(db: AsyncSession, business_id: uuid_pkg.UUID) -> list[uuid_pkg.UUID]:
    """The owner and everyone with front desk access and an account: whoever runs the bookings."""
    from sqlalchemy import or_, select

    from app.modules.business.models import Business, Staff

    business = await db.get(Business, business_id)
    front_desk = (await db.execute(
        select(Staff.user_id).where(Staff.business_id == business_id,
                                    or_(Staff.role == "front_desk", Staff.front_desk_access.is_(True)),
                                    Staff.user_id.is_not(None))
    )).scalars().all()
    return ([business.owner_id] if business else []) + list(front_desk)


@task("booking.push_update", queue="default", max_retries=3)
async def push_booking_update(
    booking_id: str, kind: str, db: AsyncSession, actor_id: Optional[str] = None, event_key: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Tell the right people about one booking event. `actor_id` (whoever caused it) is left out."""
    from app.modules.booking.messages import business_message, customer_message, load_context
    from app.modules.business.models import Staff
    from app.modules.notifications.dispatch import deliver, notify_contact

    if kind not in PUSH_KINDS:
        raise PermanentError(f"Unknown push kind {kind!r}")
    ctx = await load_context(db, uuid_pkg.UUID(booking_id))
    if ctx is None:
        return
    booking, extra = ctx.booking, extra or {}

    if kind in CUSTOMER_KINDS:
        message = customer_message(kind, ctx, extra)
        recipients: list = [booking.customer_id]
    else:
        message = business_message(kind, ctx, extra)
        if kind in STAFF_KINDS:
            staff = await db.get(Staff, booking.staff_id) if booking.staff_id else None
            recipients = [staff.user_id if staff else None]
        else:
            recipients = await desk_user_ids(db, booking.business_id)
            if kind in ("cancelled_by_customer", "rescheduled_by_customer") and booking.staff_id:
                staff = await db.get(Staff, booking.staff_id)
                recipients.append(staff.user_id if staff else None)
    if actor_id:
        recipients = [r for r in recipients if str(r) != actor_id]
    # Once each: a stylist who also runs the desk is on both lists.
    recipients = list(dict.fromkeys(recipients))

    # Read before deliver() commits (which expires the ORM rows).
    guest = (booking.customer_phone, booking.customer_email) if booking.customer_id is None else None
    data = {"bookingId": str(booking.id), "businessId": str(booking.business_id)}
    key = event_key or (f"booking:{booking.id}:{kind}" if kind in ONCE_KINDS
                        else f"booking:{booking.id}:{kind}:{uuid_pkg.uuid4().hex[:12]}")
    await deliver(db, recipients, type=f"booking_{kind}", title=message.title, body=message.body, data=data,
                  event_key=key, sms=message.sms, email=message.email)

    if kind in GUEST_KINDS and guest is not None and (message.sms or message.email):
        # No account: no inbox or settings, so the booking's own contact details. The confirmation
        # SMS has its own once-only path (send_booking_confirmation).
        await notify_contact(
            phone=guest[0] if message.sms else None, email=guest[1],
            sms=message.sms, subject=(message.email or {}).get("subject"), html_body=(message.email or {}).get("html"),
        )


async def push_update(
    booking_id: uuid_pkg.UUID, kind: str, actor_id: Optional[uuid_pkg.UUID] = None, **extra
) -> None:
    """Queue a booking notification; never fails the caller. `extra` is for the message
    (e.g. old_when on a reschedule)."""
    try:
        key = (f"booking:{booking_id}:{kind}" if kind in ONCE_KINDS
               else f"booking:{booking_id}:{kind}:{uuid_pkg.uuid4().hex[:12]}")
        await push_booking_update.enqueue(
            booking_id=str(booking_id), kind=kind, actor_id=str(actor_id) if actor_id else None,
            event_key=key, extra={k: str(v) for k, v in extra.items() if v is not None} or None,
        )
    except Exception as e:
        logger.error(f"Could not queue {kind} notification for booking {booking_id}: {e}")
