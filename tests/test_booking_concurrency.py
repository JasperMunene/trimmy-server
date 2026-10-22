"""BK-50: double-booking under real concurrency. Postgres only — the per-business, per-day advisory
lock and the exclusion constraint (D9) are what's being tested, and SQLite has neither."""
import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.security import create_access_token
from app.modules.auth.models import AuthType, User
from app.modules.business.models import Staff
from main import app
from tests.conftest import TestSessionLocal
from tests.test_create_booking import body, customer, salon  # noqa: F401  (fixtures)

pytestmark = pytest.mark.postgres_only


async def customers(db_session, n: int) -> list[dict]:
    """n customers, as auth headers (each booking is someone different, as in real life)."""
    users = [User(id=uuid.uuid4(), email=f"rush{i}.{uuid.uuid4().hex[:6]}@example.com", first_name=f"C{i}",
                  auth_type=AuthType.EMAIL, verified=True) for i in range(n)]
    db_session.add_all(users)
    await db_session.commit()
    return [{"Authorization": f"Bearer {create_access_token(data={'sub': str(u.id), 'email': u.email})}"} for u in users]


async def rush(payload: dict, headers: list[dict]) -> list[int]:
    """Fire every request at once, each with its own session and connection (unlike the shared
    test session), the way separate API requests really run."""

    async def own_session():
        async with TestSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = own_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            responses = await asyncio.gather(*(c.post("/api/v1/bookings", json=payload, headers=h) for h in headers))
    finally:
        app.dependency_overrides.pop(get_db, None)
    for r in responses:
        assert r.status_code in (201, 409), r.text
        if r.status_code == 409:
            assert r.json()["error"] == "slot_unavailable", r.text
    return [r.status_code for r in responses]


@pytest.mark.asyncio
async def test_twenty_people_one_staff_one_slot(client, salon, db_session):
    codes = await rush(body(salon, time="11:00", staff="a"), await customers(db_session, 20))
    assert codes.count(201) == 1 and codes.count(409) == 19


@pytest.mark.asyncio
async def test_any_available_fills_exactly_the_free_staff(client, salon, db_session):
    # The salon fixture has two staff; a third makes three free at 14:00.
    db_session.add(Staff(id=uuid.uuid4(), business_id=uuid.UUID(salon["id"]), name="Achieng", role="staff"))
    await db_session.commit()
    codes = await rush(body(salon, time="14:00", staff=None), await customers(db_session, 6))
    assert codes.count(201) == 3 and codes.count(409) == 3
