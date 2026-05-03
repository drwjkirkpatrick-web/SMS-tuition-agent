"""
alembic/env.py — Migration environment
═══════════════════════════════════════════════════

This file tells Alembic how to connect to the database and generate
migrations. We use the async DATABASE_URL from settings but convert
it to a sync URL for Alembic's command-line tools.
═══════════════════════════════════════════════════
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import our settings and models
from infra.settings import get_settings
from infra.database import Base
from domain import models  # ensure all models are registered

# this is the Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# target_metadata is used for autogenerate support
target_metadata = Base.metadata


def get_sync_database_url() -> str:
    """
    Convert asyncpg URL to psycopg2 (sync) URL for Alembic CLI.
    E.g., postgresql+asyncpg:// → postgresql+psycopg2://
    """
    settings = get_settings()
    url = settings.database_url.get_secret_value()
    return url.replace("+asyncpg", "+psycopg2")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_sync_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    url = get_sync_database_url()
    connectable = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,  # detect column type changes
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
