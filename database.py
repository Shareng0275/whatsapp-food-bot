"""SQLAlchemy engine and session factory.

Reads DATABASE_URL from the environment.  Falls back to a local PostgreSQL
default so ``simulate.py`` works out-of-the-box on a dev machine.

Usage
-----
    from database import get_db

    with get_db() as db:
        repo = Repository(db)
        ...
"""
import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session as SASession, DeclarativeBase

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///dev.db",
)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def get_db():
    """Yield a SQLAlchemy session and guarantee cleanup.

    On normal exit the session is committed; on exception it is rolled back.
    """
    db: SASession = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
