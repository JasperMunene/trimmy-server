import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.registry import periodic

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
