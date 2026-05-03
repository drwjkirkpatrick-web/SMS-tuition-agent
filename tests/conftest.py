"""
tests/conftest.py — Shared test fixtures (Step 4 update)
═══════════════════════════════════════════════════

Now with real async database session and HTTP client fixtures.
═══════════════════════════════════════════════════
"""

import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from infra.database import Base, async_engine


@pytest_asyncio.fixture(scope="session")
async def db_engine():
    """
    Creates a fresh database schema once per test session.
    """
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_engine
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await async_engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """
    Yields an async session rolled back after each test.
    Uses nested transactions so the global schema is preserved.
    """
    async with db_engine.connect() as conn:
        # Begin a nested transaction (SAVEPOINT)
        trans = await conn.begin_nested()
        
        session_factory = async_sessionmaker(conn, expire_on_commit=False, class_=AsyncSession)
        async with session_factory() as session:
            yield session
        
        await trans.rollback()


@pytest_asyncio.fixture
async def api_client():
    """
    Async HTTP client for testing FastAPI endpoints.
    """
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
def faker():
    from faker import Faker
    return Faker()
