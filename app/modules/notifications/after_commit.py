"""Run a notification only if the transaction that caused it commits.

    notify_after_commit(db, lambda: push_update(booking.id, "hold_expired"))

For code that changes state deep inside a shared helper (e.g. expire_holds) whose caller decides
when to commit: nothing is sent for a change that's rolled back, and nothing has to be threaded back
up to the caller.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_KEY = "notify_after_commit"
_running: set[asyncio.Task] = set()  # strong references until each finishes


def notify_after_commit(db: AsyncSession, send: Callable[[], Awaitable[Any]]) -> None:
    session = db.sync_session
    if not session.in_transaction():
        # Rollback events only fire for a transaction that exists; begin one (no I/O until it's
        # used) so a rollback always discards what was queued here.
        session.begin()
    session.info.setdefault(_KEY, []).append(send)


async def _run(send: Callable[[], Awaitable[Any]]) -> None:
    try:
        await send()
    except Exception:
        logger.exception("After-commit notification failed")


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    for send in session.info.pop(_KEY, []):
        try:
            task = asyncio.get_running_loop().create_task(_run(send))
        except RuntimeError:  # no loop (sync use): nothing to schedule on
            continue
        _running.add(task)
        task.add_done_callback(_running.discard)


# "soft" also covers a rollback() that ends the transaction without touching the database.
@event.listens_for(Session, "after_soft_rollback")
def _after_rollback(session: Session, previous_transaction) -> None:
    session.info.pop(_KEY, None)
