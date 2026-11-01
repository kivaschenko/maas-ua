"""Assemble persistence dependencies without connecting or migrating on import."""

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker


def make_session_factory(
    database_url: str | URL | None = None,
) -> sessionmaker[Session]:
    """Use DATABASE_URL only when no explicit URL is supplied; never use test config."""
    value = database_url if database_url is not None else os.environ.get("DATABASE_URL")
    if not value:
        raise ValueError("DATABASE_URL must be configured explicitly")
    url = make_url(value)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("persistence requires a postgresql+psycopg URL")
    engine = create_engine(url, hide_parameters=True)
    return sessionmaker(engine, autoflush=False, expire_on_commit=False)
