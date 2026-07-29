"""SQLAlchemy engine and request-independent session factory."""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config import DATABASE_URL


ENGINE_OPTIONS = {
    "pool_pre_ping": True,
    "pool_recycle": int(os.environ.get("DB_POOL_RECYCLE_SECONDS", "300")),
    "pool_size": int(os.environ.get("DB_POOL_SIZE", "5")),
    "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "10")),
    "pool_timeout": int(os.environ.get("DB_POOL_TIMEOUT_SECONDS", "30")),
}

# SQLite is useful only for local static checks and does not support the
# PostgreSQL row-locking behaviour used by the production survey.
if DATABASE_URL.startswith("sqlite"):
    ENGINE_OPTIONS = {"pool_pre_ping": True}

engine = create_engine(DATABASE_URL, **ENGINE_OPTIONS)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)

Base = declarative_base()
