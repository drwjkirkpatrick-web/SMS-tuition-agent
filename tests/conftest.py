"""
tests/conftest.py — Shared test fixtures
═══════════════════════════════════════════════════

pytest automatically discovers and uses this file.
Fixtures here provide:
  - An async database session for every test
  - A test client for FastAPI endpoints
  - Faker-generated fake data (schools, students, guardians)

Teaching note: We use `pytest-asyncio` so tests can `await`
database operations just like production code.
═══════════════════════════════════════════════════
"""

import pytest
from httpx import AsyncClient

# Placeholder imports — will resolve after Step 4–5
# from api.main import app
# from infra.database import async_session_factory


@pytest.fixture
async def db_session():
    """
    Yields an async SQLAlchemy session rolled back after each test.
    This keeps tests isolated — no test pollutes the database for others.
    """
    # TODO (Step 5): implement with async_session_factory and transaction rollback
    yield None


@pytest.fixture
async def api_client():
    """
    Async HTTP client for testing FastAPI endpoints.
    """
    # TODO (Step 4): instantiate with AsyncClient(app=app, base_url="http://test")
    yield None


@pytest.fixture
def faker():
    """
    Fake data generator for tests (names, phones, amounts).
    """
    from faker import Faker
    return Faker()
