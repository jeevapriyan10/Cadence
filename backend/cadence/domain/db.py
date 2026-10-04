"""SQLAlchemy engine, session management, and database helpers for Cadence."""

from collections.abc import Generator
from contextlib import contextmanager
import os
from typing import Optional

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from cadence.domain.models import Base

DEFAULT_DATABASE_URL = "sqlite:///./cadence.db"


def get_database_url() -> str:
    """Retrieve the database URL from DATABASE_URL env var, defaulting to SQLite."""
    return os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL


DATABASE_URL = get_database_url()


def get_engine(url: Optional[str] = None) -> Engine:
    """Create a SQLAlchemy engine configured for the given or default URL.

    When DATABASE_URL points to Postgres, normalizes the dialect and configures
    connection arguments appropriately. When pointing to SQLite, configures
    check_same_thread=False.
    """
    db_url = url or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]

    # If generic postgresql:// is specified, prefer psycopg if present, else fallback to psycopg2
    if db_url.startswith("postgresql://"):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            try:
                import psycopg2  # noqa: F401
                db_url = "postgresql+psycopg2://" + db_url[len("postgresql://"):]
            except ImportError:
                pass

    connect_args = {}
    engine_kwargs = {}

    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    elif db_url.startswith("postgresql"):
        engine_kwargs["pool_pre_ping"] = True

    return create_engine(db_url, connect_args=connect_args, **engine_kwargs)


engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)


@contextmanager
def get_session(session_factory: Optional[sessionmaker[Session]] = None) -> Generator[Session, None, None]:
    """Context manager for database sessions with automatic commit/rollback."""
    factory = session_factory or SessionLocal
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all_tables(bind_engine: Optional[Engine] = None) -> None:
    """Create all domain tables in the database."""
    target_engine = bind_engine or engine
    Base.metadata.create_all(bind=target_engine)


def drop_all_tables(bind_engine: Optional[Engine] = None) -> None:
    """Drop all domain tables from the database."""
    target_engine = bind_engine or engine
    Base.metadata.drop_all(bind=target_engine)


def get_db() -> Generator[Session, None, None]:
    """Dependency generator for FastAPI route handlers."""
    with get_session() as session:
        yield session
