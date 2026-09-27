"""SQLAlchemy engine, session management, and database helpers for Cadence."""

from collections.abc import Generator
from contextlib import contextmanager
import os
from typing import Optional

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from cadence.domain.models import Base

DEFAULT_DATABASE_URL = "sqlite:///cadence.db"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_engine(url: Optional[str] = None) -> Engine:
    """Create a SQLAlchemy engine configured for the given or default URL."""
    db_url = url or DATABASE_URL
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(db_url, connect_args=connect_args)


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
