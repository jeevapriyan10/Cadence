"""Unit tests verifying database configuration and connection string handling in db.py."""

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from cadence.domain.db import (
    DEFAULT_DATABASE_URL,
    create_all_tables,
    drop_all_tables,
    get_database_url,
    get_db,
    get_engine,
    get_session,
)


def test_default_sqlite_when_database_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm db.py defaults to SQLite when DATABASE_URL is unset in the environment."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    resolved_url = get_database_url()
    assert resolved_url == DEFAULT_DATABASE_URL
    assert resolved_url == "sqlite:///./cadence.db"

    engine: Engine = get_engine()
    assert engine.url.drivername == "sqlite"
    assert engine.url.database is not None and "cadence.db" in engine.url.database


def test_default_sqlite_when_database_url_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm db.py falls back to SQLite when DATABASE_URL is set to an empty string."""
    monkeypatch.setenv("DATABASE_URL", "")

    resolved_url = get_database_url()
    assert resolved_url == DEFAULT_DATABASE_URL

    engine: Engine = get_engine()
    assert engine.url.drivername == "sqlite"


def test_postgres_database_url_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm db.py correctly parses and configures a Postgres-style DATABASE_URL."""
    postgres_url = "postgresql://cadence_user:secret_pass@db.example.com:5432/cadence_prod"
    monkeypatch.setenv("DATABASE_URL", postgres_url)

    resolved_url = get_database_url()
    assert resolved_url == postgres_url

    engine: Engine = get_engine()
    assert engine.url.drivername in ("postgresql", "postgresql+psycopg2")
    assert engine.url.username == "cadence_user"
    assert engine.url.password == "secret_pass"
    assert engine.url.host == "db.example.com"
    assert engine.url.port == 5432
    assert engine.url.database == "cadence_prod"
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "psycopg2"


def test_postgres_legacy_scheme_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm legacy postgres:// URLs are rewritten to postgresql:// so SQLAlchemy loads correctly."""
    legacy_url = "postgres://cadence_user:secret_pass@db.internal:5432/cadence_cluster"
    monkeypatch.setenv("DATABASE_URL", legacy_url)

    engine: Engine = get_engine()
    assert engine.url.drivername in ("postgresql", "postgresql+psycopg2")
    assert engine.url.host == "db.internal"
    assert engine.url.database == "cadence_cluster"
    assert engine.dialect.name == "postgresql"


def test_explicit_url_takes_precedence_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm passing an explicit URL to get_engine overrides the DATABASE_URL env var."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://cadence:secret@localhost:5432/cadence")

    explicit_engine = get_engine("sqlite:///:memory:")
    assert explicit_engine.url.drivername == "sqlite"
    assert explicit_engine.url.database == ":memory:"


def test_session_management_and_table_helpers() -> None:
    """Ensure existing function signatures (get_session, create_all_tables, drop_all_tables, get_db) remain preserved."""
    test_engine = get_engine("sqlite:///:memory:")
    create_all_tables(bind_engine=test_engine)

    test_factory = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=test_engine)

    # Test get_session contextmanager commit path
    with get_session(session_factory=test_factory) as session:
        assert isinstance(session, Session)

    # Test get_session contextmanager rollback on error
    with pytest.raises(ValueError, match="simulated failure"):
        with get_session(session_factory=test_factory) as session:
            raise ValueError("simulated failure")

    # Test get_db generator
    db_gen = get_db()
    session_from_gen = next(db_gen)
    assert isinstance(session_from_gen, Session)
    with pytest.raises(StopIteration):
        next(db_gen)

    drop_all_tables(bind_engine=test_engine)
