"""Customers reviewing a completed visit (booking.json#create-review)."""
import uuid as uuid_pkg
from datetime import timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.booking.models import Booking, BookingStatus
from app.modules.booking.schemas import BookingReviewResponse
from app.modules.business.models import Review, Staff


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def review_response(r: Review) -> BookingReviewResponse:
    created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
    return BookingReviewResponse(reviewId=r.id, authorName=r.author_name, rating=r.rating, text=r.text, createdAt=created)


def author_name(booking: Booking, user: User) -> str:
    """ "Amina W": first name and last initial, like the contract's example."""
    if user.first_name:
        return f"{user.first_name} {user.last_name[0]}" if user.last_name else user.first_name
    return booking.customer_name or "Customer"


async def refresh_staff_rating(db: AsyncSession, staff_id: uuid_pkg.UUID) -> None:
    """Staff.rating = the average of their reviews, one decimal (the business's is worked out on read)."""
    avg = (await db.execute(select(func.avg(Review.rating)).where(Review.staff_id == staff_id))).scalar_one()
    staff = await db.get(Staff, staff_id)
    if staff is not None:
        staff.rating = round(float(avg), 1) if avg is not None else 0.0


class ReviewsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, user: User, booking_id: str, rating: int, text: Optional[str]) -> BookingReviewResponse:
        try:
            bid = uuid_pkg.UUID(str(booking_id))
        except ValueError:
            raise _error(404, "not_found", "No booking found for this id")
        booking = await self.db.get(Booking, bid)
        if booking is None or booking.customer_id != user.id:
            raise _error(404, "not_found", "No booking found for this id")
        if booking.status != BookingStatus.COMPLETED:
            raise _error(409, "not_completed", "You can review once the appointment is done")
        if (await self.db.execute(select(Review.id).where(Review.booking_id == bid))).first():
            raise _error(409, "already_reviewed", "You've already reviewed this appointment")

        review = Review(
            id=uuid_pkg.uuid4(), business_id=booking.business_id, staff_id=booking.staff_id, booking_id=booking.id,
            customer_id=user.id, author_name=author_name(booking, user), rating=rating,
            text=(text or "").strip() or None,
        )
        self.db.add(review)
        try:
            await self.db.flush()  # the unique booking_id settles a double tap
        except IntegrityError:
            await self.db.rollback()
            raise _error(409, "already_reviewed", "You've already reviewed this appointment")
        if booking.staff_id:
            await refresh_staff_rating(self.db, booking.staff_id)
        await self.db.commit()
        await self.db.refresh(review)
        return review_response(review)
