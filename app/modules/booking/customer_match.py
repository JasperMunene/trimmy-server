"""Linking bookings a business makes (walk-ins, on someone's behalf) to the customer's Trimmy account,
so they see the booking in their app and get its notifications.

Only proven contact details count, so nobody can collect someone else's bookings by typing their
number into a profile:
- a phone, once verified with a code (User.phone_verified_at);
- an email the account signed up with and verified, or got from Google. (Emails can't be changed
  later; one added during phone signup isn't verified, so it doesn't count.)

Two directions:
- match_account(): when the booking is made, find the account now;
- claim_bookings(): when someone proves a phone or email later (signs up after their visit), take
  over the recent bookings made for it.
"""
import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import AuthType, User

# Bookings older than this aren't claimed at signup: phone numbers get recycled.
CLAIM_WINDOW = timedelta(days=180)


def _trusted_email(user: User) -> bool:
    return bool(user.email) and user.verified and user.auth_type in (AuthType.EMAIL, AuthType.GOOGLE)


async def match_account(db: AsyncSession, phone: Optional[str], email: Optional[str]) -> Optional[uuid_pkg.UUID]:
    """The account these contact details prove, if any. Phone first: it's what businesses take."""
    if phone:
        uid = (await db.execute(
            select(User.id).where(User.phone == phone, User.phone_verified_at.is_not(None))
        )).scalar_one_or_none()
        if uid:
            return uid
    if email:
        uid = (await db.execute(
            select(User.id).where(
                func.lower(User.email) == email.strip().lower(), User.verified.is_(True),
                User.auth_type.in_([AuthType.EMAIL, AuthType.GOOGLE]),
            )
        )).scalar_one_or_none()
        if uid:
            return uid
    return None


async def claim_bookings(db: AsyncSession, user: User) -> int:
    """Give `user` the recent account-less bookings made for their proven phone or email. The caller
    commits. Returns how many."""
    from app.modules.booking.models import Booking

    matches = []
    if user.phone and user.phone_verified_at:
        matches.append(Booking.customer_phone == user.phone)
    if _trusted_email(user):
        matches.append(func.lower(Booking.customer_email) == user.email.lower())
    if not matches:
        return 0
    result = await db.execute(
        update(Booking)
        .where(Booking.customer_id.is_(None), or_(*matches),
               Booking.created_at >= datetime.now(timezone.utc) - CLAIM_WINDOW)
        .values(customer_id=user.id)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0
