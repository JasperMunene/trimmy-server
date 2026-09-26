"""Booking state machine (docs/booking/PLAN.md, section 4.2).

Every status change goes through `transition()` (or `record_created()` for the
first status), which checks the move is allowed and writes a BookingEvent.
Nothing else should assign `booking.status`.

    pending_payment -> confirmed     M-Pesa callback success
    pending_payment -> cancelled     hold expired / customer cancels
    confirmed       -> in_progress   check-in / start
    confirmed       -> no_show       marked by the business
    confirmed       -> cancelled     cancel within policy
    in_progress     -> completed     marked complete
"""
import uuid as uuid_pkg
from typing import Optional

from app.modules.booking.models import Booking, BookingEvent, BookingStatus as S

INITIAL = frozenset({S.PENDING_PAYMENT, S.CONFIRMED, S.IN_PROGRESS})  # in_progress: walk-ins

TRANSITIONS: dict[str, frozenset[str]] = {
    S.PENDING_PAYMENT: frozenset({S.CONFIRMED, S.CANCELLED}),
    S.CONFIRMED: frozenset({S.IN_PROGRESS, S.NO_SHOW, S.CANCELLED}),
    S.IN_PROGRESS: frozenset({S.COMPLETED}),
    S.COMPLETED: frozenset(),
    S.NO_SHOW: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, from_status: Optional[str], to_status: str):
        self.from_status, self.to_status = from_status, to_status
        super().__init__(f"A booking can't go from {from_status or 'nothing'} to {to_status}")


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in TRANSITIONS.get(from_status, frozenset())


def record_created(
    booking: Booking, status: str, *, actor_id: Optional[uuid_pkg.UUID] = None, reason: Optional[str] = None
) -> None:
    """Set a new booking's first status and log it."""
    if status not in INITIAL:
        raise InvalidTransition(None, status)
    booking.status = status
    booking.events.append(BookingEvent(from_status=None, to_status=status, actor_id=actor_id, reason=reason))


def transition(
    booking: Booking, to_status: str, *, actor_id: Optional[uuid_pkg.UUID] = None, reason: Optional[str] = None
) -> None:
    """Move a booking to `to_status`, or raise InvalidTransition. The caller commits.

    `reason` is stored on the event, and also as `cancel_reason` when cancelling
    (e.g. "payment_expired", "customer", "business").
    """
    if not can_transition(booking.status, to_status):
        raise InvalidTransition(booking.status, to_status)
    from_status = booking.status
    booking.status = to_status
    if to_status == S.CANCELLED:
        booking.cancel_reason = reason
    if to_status != S.PENDING_PAYMENT:
        booking.expires_at = None  # the hold no longer applies
    booking.events.append(BookingEvent(from_status=from_status, to_status=to_status, actor_id=actor_id, reason=reason))
