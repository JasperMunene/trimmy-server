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
from app.modules.business.schemas import ListReviewsResponse, ReviewReplyResponse, ReviewResponse

MAX_PAGE = 50


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def review_response(r: Review) -> BookingReviewResponse:
    return BookingReviewResponse(
        reviewId=r.id, authorName=r.author_name, rating=r.rating, text=r.text, createdAt=_aware(r.created_at),
        replyText=r.reply_text, repliedAt=_aware(r.replied_at),
    )


async def review_details(db: AsyncSession, reviews: list[Review]) -> list[ReviewResponse]:
    """Reviews with the reviewer's photo, who did the service, what was done and when, the business
    name and the reply. A fixed number of queries however many reviews (lists are paged)."""
    from app.modules.booking.models import BookingLine
    from app.modules.business.models import Business

    if not reviews:
        return []
    user_ids = {r.customer_id for r in reviews if r.customer_id}
    staff_ids = {r.staff_id for r in reviews if r.staff_id}
    booking_ids = {r.booking_id for r in reviews if r.booking_id}
    business_ids = {r.business_id for r in reviews}
    avatars = dict((await db.execute(select(User.id, User.avatar_url).where(User.id.in_(user_ids)))).all()) if user_ids else {}
    staff = {
        s.id: s for s in (await db.execute(select(Staff).where(Staff.id.in_(staff_ids)))).scalars()
    } if staff_ids else {}
    visit_dates = dict(
        (await db.execute(select(Booking.id, Booking.local_date).where(Booking.id.in_(booking_ids)))).all()
    ) if booking_ids else {}
    services: dict = {}
    if booking_ids:
        lines = await db.execute(
            select(BookingLine.booking_id, BookingLine.name)
            .where(BookingLine.booking_id.in_(booking_ids)).order_by(BookingLine.position)
        )
        for booking_id, name in lines.all():
            services.setdefault(booking_id, []).append(name)
    names = dict((await db.execute(select(Business.id, Business.name).where(Business.id.in_(business_ids)))).all())

    out = []
    for r in reviews:
        member = staff.get(r.staff_id)
        out.append(ReviewResponse(
            reviewId=r.id, authorName=r.author_name, authorAvatarUrl=avatars.get(r.customer_id),
            rating=r.rating, text=r.text or "", createdAt=_aware(r.created_at),
            businessId=r.business_id, businessName=names.get(r.business_id),
            bookingId=r.booking_id, staffId=r.staff_id,
            staffName=member.name if member else None, staffAvatarUrl=member.avatar_url if member else None,
            services=services.get(r.booking_id, []), visitDate=visit_dates.get(r.booking_id),
            reply=ReviewReplyResponse(text=r.reply_text, repliedAt=_aware(r.replied_at)) if r.reply_text else None,
        ))
    return out


def author_name(booking: Booking, user: User) -> str:
    """ "Amina W": first name and last initial, like the contract's example."""
    if user.first_name:
        return f"{user.first_name} {user.last_name[0]}" if user.last_name else user.first_name
    return booking.customer_name or "Customer"


async def refresh_staff_rating(db: AsyncSession, staff_id: uuid_pkg.UUID) -> None:
    """Staff.rating = the average of their reviews, one decimal (the business's is worked out on read)."""
    avg = (await db.execute(
        select(func.avg(Review.rating)).where(Review.staff_id == staff_id, Review.hidden_at.is_(None))
    )).scalar_one()
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

    # ─── reading ─────────────────────────────────────────────

    async def for_business(
        self, business_id: str, staff_id: Optional[str], unreplied: bool, limit: int, offset: int,
    ) -> ListReviewsResponse:
        """A business's reviews, newest first: public (its profile's "See all"), filterable by who
        did the service; the business uses unreplied=true to find ones to answer."""
        from app.modules.business.models import Business

        try:
            bid = uuid_pkg.UUID(str(business_id))
            sid = uuid_pkg.UUID(str(staff_id)) if staff_id else None
        except ValueError:
            raise _error(404, "not_found", "No business found for this id")
        if await self.db.get(Business, bid) is None:
            raise _error(404, "not_found", "No business found for this id")
        where = [Review.business_id == bid, Review.hidden_at.is_(None)]  # hidden by Trimmy's team: never public
        if sid:
            where.append(Review.staff_id == sid)
        if unreplied:
            where.append(Review.reply_text.is_(None))
        return await self._page(where, limit, offset)

    async def mine(self, user: User, limit: int, offset: int) -> ListReviewsResponse:
        """The reviews this customer has written, newest first, with any replies."""
        return await self._page([Review.customer_id == user.id], limit, offset)

    async def _page(self, where: list, limit: int, offset: int) -> ListReviewsResponse:
        limit = max(1, min(limit, MAX_PAGE))
        total, average = (
            await self.db.execute(select(func.count(), func.avg(Review.rating)).where(*where))
        ).one()
        rows = (
            await self.db.execute(
                select(Review).where(*where).order_by(Review.created_at.desc(), Review.id).limit(limit).offset(max(offset, 0))
            )
        ).scalars().all()
        return ListReviewsResponse(
            reviews=await review_details(self.db, list(rows)), total=total,
            averageRating=round(float(average), 1) if average is not None else 0.0,
        )

    # ─── replying (owner or front desk) ──────────────────────

    async def reply(self, user: User, review_id: str, text: Optional[str]) -> ReviewResponse:
        """Set (or, with text None, remove) the business's reply. The customer is told about a new one."""
        from datetime import datetime

        from app.modules.booking.business_bookings import BusinessBookingsService

        try:
            rid = uuid_pkg.UUID(str(review_id))
        except ValueError:
            raise _error(404, "not_found", "No review found for this id")
        review = await self.db.get(Review, rid)
        if review is None:
            raise _error(404, "not_found", "No review found for this id")
        await BusinessBookingsService(self.db)._require_manager(user, review.business_id)

        first_reply = review.reply_text is None and text is not None
        review.reply_text = text.strip() if text else None
        review.replied_at = datetime.now(timezone.utc) if text else None
        review.replied_by = user.id if text else None
        await self.db.commit()
        await self.db.refresh(review)
        if first_reply and review.customer_id:
            from app.modules.business.models import Business
            from app.modules.notifications.tasks import notify

            business = await self.db.get(Business, review.business_id)
            await notify(
                review.customer_id, f"{business.name if business else 'The business'} replied to your review",
                review.reply_text[:140], type="review_replied", reviewId=review.id, businessId=review.business_id,
            )
        return (await review_details(self.db, [review]))[0]
