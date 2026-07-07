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

from config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,  # avoids stale-connection errors on free-tier DBs that idle
)

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
    Runs automatically on app startup (see main.py) when AUTO_CREATE_TABLES=true.
    """
    async with engine.begin() as conn:
        import models  # noqa: F401 -- admin/bot tables
        import community.models  # noqa: F401 -- community accounts table
        await conn.run_sync(Base.metadata.create_all)
