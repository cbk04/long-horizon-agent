"""SQLAlchemy engine, session, and Base declaration."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.mysql_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yield a database session and ensure it's closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
