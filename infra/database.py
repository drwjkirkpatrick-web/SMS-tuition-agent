"""
infra/database.py — Async PostgreSQL connection management
═══════════════════════════════════════════════════

SQLAlchemy 2.0+ supports native async via asyncpg. This file creates:
  - `async_engine` — connection pool to PostgreSQL
  - `async_session_factory` — creates async sessions for transactions
  - `Base` — declarative base for ORM models (Step 5)

Teaching notes:
  - `pool_pre_ping=True` checks connections before use (prevents
    "connection closed" errors after network blips).
  - `expire_on_commit=False` keeps objects usable after a transaction
    commits — essential for the outbox pattern (Step 10).
  - We use Unix sockets in production (faster, no TCP overhead on Pi).
═══════════════════════════════════════════════════
"""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from infra.settings import get_settings

# Parse the async database URL from settings
_settings = get_settings()
DATABASE_URL = _settings.database_url.get_secret_value()

# Create the async engine
# pool_pre_ping=True: verifies connection is alive before checkout
async_engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    # Small pool for Raspberry Pi (default 5 connections max)
    pool_size=5,
    max_overflow=0,
    echo=_settings.app_env == "development",  # log SQL in dev
)

# Session factory: creates async sessions on demand
# autocommit=False, autoflush=False: we control transactions explicitly
async_session_factory = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,   # keep objects attached after commit
    autocommit=False,
    autoflush=False,
)

# Declarative base: all ORM models inherit from this
Base = declarative_base()


async def get_db():
    """
    FastAPI dependency: yields an async database session.
    Automatically closes the session when the request ends.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """
    Creates all tables on startup.
    In production, prefer Alembic migrations (Step 5) instead.
    """
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """Dispose the engine pool on shutdown."""
    await async_engine.dispose()


# Import all models so Base.metadata includes them
# This MUST happen after Base is defined
from domain import models  # noqa: F401,E402
