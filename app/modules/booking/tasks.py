import asyncio
import logging
import uuid as uuid_pkg
from datetime import datetime, timezone
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
    """Queue the confirmation SMS; call after the commit that confirmed the booking."""
    try:
        await send_booking_confirmation.enqueue(booking_id=str(booking_id))
    except Exception as e:  # never fail the request over a notification
        logger.error(f"Could not queue confirmation for booking {booking_id}: {e}")
