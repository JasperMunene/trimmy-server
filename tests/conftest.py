import asyncio
import os
import tempfile
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# Never touch the real R2 bucket from tests, even though .env configures it.
os.environ["STORAGE_BACKEND"] = "local"
os.environ["MEDIA_ROOT"] = tempfile.mkdtemp(prefix="trimmy-test-media-")

from app.core.base import Base
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.core.security import create_access_token, get_password_hash
from app.modules.auth.models import AuthType, User
from main import app

# Tests run on in-memory SQLite by default. Set TEST_DATABASE_URL to a throwaway
# Postgres (postgresql+asyncpg://...) to run them there too, as CI does (BK-05).
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///:memory:"
ON_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")

if ON_POSTGRES:
    # Every test drops all tables. Refuse anything that isn't obviously a local or CI
    # database, so a copied staging/production URL can never be wiped by a test run.
    _host = make_url(TEST_DATABASE_URL).host or ""
    if _host not in ("localhost", "127.0.0.1", "postgres") and os.environ.get("ALLOW_REMOTE_TEST_DB") != "1":
        raise RuntimeError(
            f"TEST_DATABASE_URL points at {_host!r}; tests drop every table, so only localhost/127.0.0.1/postgres "
            "are allowed (set ALLOW_REMOTE_TEST_DB=1 to override for a disposable remote database)"
        )
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
else:
    engine = create_async_engine(TEST_DATABASE_URL)
TestSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


def pytest_collection_modifyitems(config, items):
    if ON_POSTGRES:
        return
    skip = pytest.mark.skip(reason="needs Postgres: set TEST_DATABASE_URL (runs in CI)")
    for item in items:
        if "postgres_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def reset_rate_limits():
    limiter.reset()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        if ON_POSTGRES:
            # The bookings exclusion constraint (D9) needs it.
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session():
    async with TestSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession):
    user = User(
        id=uuid.uuid4(),
        email="test@example.com",
        password_hash=get_password_hash("testpass123"),
        first_name="Test",
        last_name="User",
        phone="+1234567890",
        auth_type=AuthType.EMAIL,
        verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def unverified_user(db_session: AsyncSession):
    user = User(
        id=uuid.uuid4(),
        email="unverified@example.com",
        password_hash=get_password_hash("testpass123"),
        first_name="Unverified",
        last_name="User",
        auth_type=AuthType.EMAIL,
        verified=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
def auth_headers(test_user: User):
    token = create_access_token(data={"sub": str(test_user.id), "email": test_user.email})
    return {"Authorization": f"Bearer {token}"}
