"""
Database engine and session setup (SQLAlchemy 2.0, async).

This module owns:
- the async engine connected to Postgres,
- the session factory used by request handlers,
- the declarative Base that all ORM models inherit from,
- an init_db() helper to create tables on startup.
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# echo=False in production; flip to True locally if you want to see the
# generated SQL in your terminal while debugging.
engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,  # avoids stale-connection errors on free-tier DBs that idle
)

# Each request gets its own AsyncSession from this factory.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class every ORM model inherits from."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields a database session and guarantees
    it's closed afterwards, even if the request raises an exception.
    """
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """
    Create all tables defined on Base's metadata if they don't already exist.

    This is the "how to initialize the database tables" step referenced in
    the task: it runs automatically on app startup (see main.py) when
    AUTO_CREATE_TABLES=true. It is equivalent to running, once, against your
    Render Postgres instance:

        CREATE TABLE IF NOT EXISTS users (...);
        CREATE TABLE IF NOT EXISTS logs (...);

    For a real production workflow you'd typically replace this with
    Alembic migrations, but this is perfectly fine for getting started.
    """
    async with engine.begin() as conn:
        # Import models here so they're registered on Base.metadata
        # before create_all is called.
        from app import models  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
