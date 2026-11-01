"""Configuration tests never connect to a database."""

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from maas_ua.bootstrap import make_session_factory


def test_test_database_url_is_never_an_application_fallback(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://example/maas_ua_test")
    with pytest.raises(ValueError, match="DATABASE_URL must be configured"):
        make_session_factory()


def test_explicit_application_url_wins_over_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example/environment")
    factory = make_session_factory("postgresql+psycopg://example/explicit")
    engine = factory.kw["bind"]
    try:
        assert engine.url.database == "explicit"
        assert engine.hide_parameters
        assert factory.kw["autoflush"] is False
    finally:
        engine.dispose()


def test_application_uses_database_url_without_connecting(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example/configured")
    factory = make_session_factory()
    engine = factory.kw["bind"]
    try:
        assert engine.url.database == "configured"
    finally:
        engine.dispose()


@pytest.mark.parametrize("url", ["", "sqlite://", "postgresql://example/db"])
def test_rejects_missing_or_unsupported_application_url(url):
    with pytest.raises(ValueError):
        make_session_factory(url)


def test_migrations_never_fall_back_to_test_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://example/maas_ua_test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.output_buffer = StringIO()
    with pytest.raises(ValueError, match="DATABASE_URL must be configured"):
        command.upgrade(config, "head", sql=True)
