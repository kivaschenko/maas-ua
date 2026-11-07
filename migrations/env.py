"""Explicit PostgreSQL configuration; no implicit .env or test URL fallback."""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from maas_ua.adapters.accounting_orm import Base

config = context.config
target_metadata = Base.metadata


def database_url():
    # A programmatic override lets tests migrate only their freshly created DB.
    value = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
    if not value:
        raise ValueError("DATABASE_URL must be configured explicitly for migrations")
    url = make_url(value)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("migrations require a postgresql+psycopg URL")
    return url


def run_migrations_offline():
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_engine(database_url(), poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
