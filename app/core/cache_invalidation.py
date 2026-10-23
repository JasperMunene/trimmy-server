"""Clears cached discovery data (app/core/cache.py) when what it was built from changes.

One hook on every Session (API and workers), instead of a call at each place that saves:
- after each flush, note which businesses the new / changed / deleted rows belong to;
- after the commit, drop those businesses' cached profiles and the search cards. A rollback
  forgets the notes.
Changes that can't be traced to one business retire every cached profile at once (a generation
bump): bulk UPDATE/DELETE statements on these tables (they skip the per-row bookkeeping), and a
person's name or photo changing (shown on staff and on reviews they wrote).

Imported once at startup (main.py, worker.py) for its side effect: registering the listeners.
"""
import asyncio
import logging

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app.core import cache
from app.core.config import settings
from app.modules.auth.models import User
from app.modules.business.models import (
    Business, BusinessPhoto, BusinessPolicies, Review, Service, ServiceCategory, Staff,
    StaffPortfolioPhoto, StaffService, WorkingHours,
)

logger = logging.getLogger(__name__)

# Rows with a business_id that feed search cards or profiles.
_BY_BUSINESS = (BusinessPhoto, BusinessPolicies, Review, Service, ServiceCategory, Staff, WorkingHours)
# Rows that only know their staff member.
_BY_STAFF = (StaffPortfolioPhoto, StaffService)
_TABLES = {m.__tablename__ for m in (Business, *_BY_BUSINESS, *_BY_STAFF)}
_PERSON_FIELDS = ("first_name", "last_name", "avatar_url")

_pending_tasks: set = set()  # keeps fire-and-forget clears alive until they finish


def _notes(session: Session) -> dict:
    return session.info.setdefault("discovery_cache", {"businesses": set(), "everything": False})


@event.listens_for(Session, "after_flush")
def _note_changes(session: Session, _context) -> None:
    notes = _notes(session)
    staff_ids = set()
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, Business):
            notes["businesses"].add(obj.id)
        elif isinstance(obj, _BY_BUSINESS):
            notes["businesses"].add(obj.business_id)
        elif isinstance(obj, _BY_STAFF):
            staff_ids.add(obj.staff_id)
        elif isinstance(obj, User) and obj in session.dirty:
            state = inspect(obj)
            if any(state.attrs[f].history.has_changes() for f in _PERSON_FIELDS):
                notes["everything"] = True
    staff_ids.discard(None)
    if staff_ids:
        rows = session.execute(select(Staff.business_id).where(Staff.id.in_(staff_ids))).scalars()
        notes["businesses"].update(rows)
    notes["businesses"].discard(None)


@event.listens_for(Session, "do_orm_execute")
def _note_bulk_statements(state) -> None:
    if not (state.is_update or state.is_delete or state.is_insert):
        return
    table = getattr(state.statement, "table", None)
    if getattr(table, "name", None) in _TABLES:
        _notes(state.session)["everything"] = True


@event.listens_for(Session, "after_rollback")
def _forget(session: Session) -> None:
    session.info.pop("discovery_cache", None)


@event.listens_for(Session, "after_commit")
def _clear_after_commit(session: Session) -> None:
    notes = session.info.pop("discovery_cache", None)
    if not notes or not (notes["businesses"] or notes["everything"]):
        return
    if settings.CACHE_BACKEND == "off":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop (a sync script): the TTLs catch up
    task = loop.create_task(_clear(notes))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def _clear(notes: dict) -> None:
    gen = await cache.generation()
    keys = [cache.CARDS_KEY, *(cache.profile_key(gen, str(bid)) for bid in notes["businesses"])]
    await cache.delete(*keys)
    if notes["everything"]:
        await cache.bump_generation()


async def settle() -> None:
    """Tests: wait for clears scheduled by commits so far."""
    while _pending_tasks:
        await asyncio.gather(*list(_pending_tasks))
