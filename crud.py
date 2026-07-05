"""
Query functions ("CRUD" layer) used by the dashboard routes.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Log, User


async def get_total_users(db: AsyncSession) -> int:
    """Total number of rows in the users table."""
    result = await db.execute(select(func.count()).select_from(User))
    return result.scalar_one()


async def get_active_users_24h(db: AsyncSession) -> int:
    """Count of DISTINCT user_id values in logs within the last 24 hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    result = await db.execute(
        select(func.count(func.distinct(Log.user_id))).where(Log.timestamp >= since)
    )
    return result.scalar_one()


async def get_mini_app_opens(db: AsyncSession) -> int:
    """Count of log rows where action == 'Opened Mini App'."""
    result = await db.execute(
        select(func.count()).select_from(Log).where(Log.action == "Opened Mini App")
    )
    return result.scalar_one()


async def get_recent_logs(db: AsyncSession, limit: int = 10) -> list[Log]:
    """The most recent `limit` logs, newest first."""
    result = await db.execute(
        select(Log).order_by(Log.timestamp.desc()).limit(limit)
    )
    return list(result.scalars().all())
