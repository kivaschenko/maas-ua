"""Create only uniquely named disposable databases on the explicit test server."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def database_url():
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("set TEST_DATABASE_URL to the isolated PostgreSQL test service")
    url = make_url(raw_url)
    if (
        url.drivername != "postgresql+psycopg"
        or url.database != "maas_ua_test"
        or url.username != "maas_test"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
    ):
        pytest.fail("TEST_DATABASE_URL must select the local maas_test/maas_ua_test service")

    # Never migrate, truncate, or drop the database supplied by the environment.
    # Only this fixture's newly created, random database is disposable.
    name = f"maas_ua_test_{uuid4().hex}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            yield url.set(database=name)
        finally:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture
def migration_config(database_url):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def engine(database_url, migration_config):
    command.upgrade(migration_config, "head")
    engine = create_engine(database_url, hide_parameters=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(engine, autoflush=False, expire_on_commit=False)
