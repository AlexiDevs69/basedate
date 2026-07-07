"""
ORM models for the public-facing "community" side of the project --
registration, login, public profiles, and (later) posts/feed.

Lives in its own table namespace (community_accounts) so it never collides
with the bot's users/logs tables or the admin's own admin_profile/bot_settings.
"""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    """
    A single registered community user. Can be created via email+password,
    via Telegram login, or (in principle) both -- whichever of those
    fields is set determines which login method(s) work for this row.
    """
    __tablename__ = "community_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # A unique handle used for login lookups, mentions, and profile URLs
    # (/community/profile/<username>).
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)

    # --- Email + password login (both nullable -- a Telegram-only account
    # simply never sets these) ---
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Telegram login (both nullable -- an email-only account never sets these) ---
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Public profile -- same shape as the admin's own profile, so the
    # same visual design (badge, gradient role tag) works for both. ---
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    banner_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    role_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role_color_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    role_color_end: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- Moderation -- managed from the admin dashboard (next step). ---
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Presence -- updated on every request while logged in; "online"
    # means last_seen_at is within the last few minutes (see crud.py). ---
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
