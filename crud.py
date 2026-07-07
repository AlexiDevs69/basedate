"""
Query functions ("CRUD" layer) used by the dashboard routes.
Keeping these separate from the route handlers makes main.py easier to read
and makes the queries independently testable/reusable.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import AdminProfile, BotSettings, Log, User


async def get_total_users(db: AsyncSession) -> int:
    """Total number of rows in the users table."""
    result = await db.execute(select(func.count()).select_from(User))
    return result.scalar_one()


async def get_active_users_24h(db: AsyncSession) -> int:
    """
    Count of DISTINCT user_id values in the logs table whose timestamp
    falls within the last 24 hours.
    """
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


PAGE_SIZE = 50


async def get_users_page(db: AsyncSession, page: int = 1) -> tuple[list[User], int]:
    """
    Returns (rows, total_count) for a page of users, newest first.
    `page` is 1-indexed.
    """
    offset = (page - 1) * PAGE_SIZE
    total = await get_total_users(db)
    result = await db.execute(
        select(User).order_by(User.created_at.desc()).limit(PAGE_SIZE).offset(offset)
    )
    return list(result.scalars().all()), total


async def get_logs_page(db: AsyncSession, page: int = 1) -> tuple[list[Log], int]:
    """
    Returns (rows, total_count) for a page of logs, newest first.
    `page` is 1-indexed.
    """
    offset = (page - 1) * PAGE_SIZE
    result_count = await db.execute(select(func.count()).select_from(Log))
    total = result_count.scalar_one()
    result = await db.execute(
        select(Log).order_by(Log.timestamp.desc()).limit(PAGE_SIZE).offset(offset)
    )
    return list(result.scalars().all()), total


async def get_all_user_ids(db: AsyncSession) -> list[int]:
    """Returns just the user_id column for every user -- used for broadcasting."""
    result = await db.execute(select(User.user_id))
    return [row[0] for row in result.all()]


async def log_broadcast(
    db: AsyncSession, admin_username: str, sent: int, failed: int, preview: str
) -> None:
    """Records a summary row in the logs table after a broadcast finishes."""
    short_preview = preview[:80] + ("…" if len(preview) > 80 else "")
    log = Log(
        user_id=0,
        username=admin_username,
        action=f'Broadcast sent to {sent} (failed {failed}): "{short_preview}"',
    )
    db.add(log)
    await db.commit()


async def get_profile(db: AsyncSession, username: str) -> AdminProfile:
    """
    Повертає єдиний профіль (id=1), створюючи його з дефолтними
    значеннями при першому зверненні (перший заход на /profile
    після деплою цієї фічі).
    """
    result = await db.execute(select(AdminProfile).where(AdminProfile.id == 1))
    profile = result.scalar_one_or_none()

    if profile is None:
        profile = AdminProfile(id=1, username=username)
        db.add(profile)
        await db.commit()
        await db.refresh(profile)

    return profile


async def update_profile(
    db: AsyncSession,
    avatar_url: str | None,
    banner_url: str | None,
    bio: str | None,
    is_verified: bool,
    role_label: str | None,
    role_color_start: str | None,
    role_color_end: str | None,
) -> AdminProfile:
    """Оновлює редаговані поля профілю (id=1). Пусті рядки перетворюються на None."""
    result = await db.execute(select(AdminProfile).where(AdminProfile.id == 1))
    profile = result.scalar_one()

    profile.avatar_url = avatar_url or None
    profile.banner_url = banner_url or None
    profile.bio = bio or None
    profile.is_verified = is_verified
    profile.role_label = role_label or None
    profile.role_color_start = role_color_start or None
    profile.role_color_end = role_color_end or None

    await db.commit()
    await db.refresh(profile)
    return profile


async def get_settings(db: AsyncSession) -> BotSettings:
    """
    Повертає єдиний рядок налаштувань (id=1), створюючи його з дефолтними
    значеннями при першому зверненні (перший заход на /settings після
    деплою цієї фічі).
    """
    result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
    settings_row = result.scalar_one_or_none()

    if settings_row is None:
        settings_row = BotSettings(id=1, maintenance_mode=False)
        db.add(settings_row)
        await db.commit()
        await db.refresh(settings_row)

    return settings_row


async def update_settings(
    db: AsyncSession,
    welcome_message: str | None,
    maintenance_mode: bool,
    maintenance_message: str | None,
) -> BotSettings:
    """Оновлює налаштування бота (id=1)."""
    result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
    settings_row = result.scalar_one()

    settings_row.welcome_message = welcome_message or None
    settings_row.maintenance_mode = maintenance_mode
    settings_row.maintenance_message = maintenance_message or None

    await db.commit()
    await db.refresh(settings_row)
    return settings_row
