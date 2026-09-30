"""SQLAlchemy engine and session factory.

Reads DATABASE_URL from the environment.
Configures SQLite with busy timeout and WAL mode for reliable concurrency during local development.

Usage
-----
    from database import get_db

    with get_db() as db:
        repo = Repository(db)
        ...
"""
import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session as SASession, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///dev.db",
)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


connect_args = {}
if "sqlite" in DATABASE_URL:
    connect_args = {"timeout": 30, "check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args=connect_args,
)

if "sqlite" in DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        finally:
            cursor.close()

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
