import asyncio
import logging
import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

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

    booking = (await db.execute(select(Booking).where(Booking.id == bid).options(selectinload(Booking.lines)))).scalar_one()
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
                Booking.customer_phone.is_not(None),
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
               Booking.customer_phone.is_not(None), Booking.starts_at > now)
        .values({column.key: now})
    )
    await db.commit()
    if claimed.rowcount == 0:
        return

    booking = (await db.execute(select(Booking).where(Booking.id == bid).options(selectinload(Booking.lines)))).scalar_one()
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
    await push_update(bid, f"reminder_{kind}")


# ─── Push notifications about a booking (BK-41) ──────────────

PUSH_KINDS = ("confirmed", "cancelled_by_business", "reminder_24h", "reminder_2h", "assigned")


@task("booking.push_update", queue="default", max_retries=3)
async def push_booking_update(booking_id: str, kind: str, db: AsyncSession) -> None:
    """Push one booking event to the right person: the customer, or for "assigned" the staff member."""
    from sqlalchemy import delete, select
    from sqlalchemy.orm import selectinload
    from zoneinfo import ZoneInfo

    from app.modules.booking.models import Booking
    from app.modules.business.models import Business, Staff
    from app.modules.notifications.models import PushToken
    from app.modules.notifications.push import message, send_expo

    if kind not in PUSH_KINDS:
        raise PermanentError(f"Unknown push kind {kind!r}")
    booking = (
        await db.execute(select(Booking).where(Booking.id == uuid_pkg.UUID(booking_id)).options(selectinload(Booking.lines)))
    ).scalar_one_or_none()
    if booking is None:
        return
    business = await db.get(Business, booking.business_id)
    staff = await db.get(Staff, booking.staff_id) if booking.staff_id else None
    tz = ZoneInfo(business.timezone if business else "Africa/Nairobi")
    starts = (booking.starts_at if booking.starts_at.tzinfo else booking.starts_at.replace(tzinfo=timezone.utc)).astimezone(tz)
    when = starts.strftime("%a %-d %b %H:%M")
    what = _what([line.name for line in booking.lines])
    name = business.name if business else "your salon"

    recipient = booking.customer_id
    if kind == "confirmed":
        title, body = "Booking confirmed", f"{name}: {what}, {when}. Ref {booking.reference}."
    elif kind == "cancelled_by_business":
        title, body = "Booking cancelled", f"{name} cancelled your {what} on {when}."
    elif kind == "reminder_24h":
        title, body = "See you tomorrow", f"{what} at {name}, {when}."
    elif kind == "reminder_2h":
        title, body = "Coming up soon", f"{what} at {name} at {starts.strftime('%H:%M')}."
    else:  # assigned: tell the staff member
        recipient = staff.user_id if staff else None
        title, body = "New booking", f"{what} with {booking.customer_name}, {when}."
    if recipient is None:
        return

    tokens = (await db.execute(select(PushToken.token).where(PushToken.user_id == recipient))).scalars().all()
    data = {"type": f"booking_{kind}", "bookingId": str(booking.id), "businessId": str(booking.business_id)}
    dead = await send_expo([message(t, title, body, data) for t in tokens])
    if dead:
        await db.execute(delete(PushToken).where(PushToken.token.in_(dead)))
        await db.commit()


async def push_update(booking_id: uuid_pkg.UUID, kind: str) -> None:
    """Queue a booking push; never fails the caller."""
    try:
        await push_booking_update.enqueue(booking_id=str(booking_id), kind=kind)
    except Exception as e:
        logger.error(f"Could not queue {kind} push for booking {booking_id}: {e}")
