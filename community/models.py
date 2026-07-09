"""
ORM models for the public-facing "community" side of the project --
registration, login, public profiles, and (later) posts/feed.

Lives in its own table namespace (community_accounts) so it never collides
with the bot's users/logs tables or the admin's own admin_profile/bot_settings.
"""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    # --- Visual identity -- Discord/Nitro-style nickname effects and card backgrounds. ---
    # Existing columns are added idempotently on startup by community_crud.ensure_account_visual_columns().
    name_effect: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name_color_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name_color_end: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name_font: Mapped[str | None] = mapped_column(String(32), nullable=True)
    profile_card_bg_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # --- Moderation -- managed from the admin dashboard (next step). ---
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Presence -- updated on every request while logged in; "online"
    # means last_seen_at is within the last few minutes (see crud.py). ---
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Friendship(Base):
    """
    A friend request/relationship between two accounts.

    status progression: pending -> accepted (or declined). A declined row
    is left in place rather than deleted, so a fresh request from either
    side just resets it back to pending instead of creating duplicates.
    """
    __tablename__ = "community_friendships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    requester_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("community_accounts.id"), nullable=False, index=True
    )
    addressee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("community_accounts.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ============================================================================
# Forum: Discord-style channels + Telegram-style flat posts + Reddit-style
# likes and single-level comments.
# ============================================================================

class Channel(Base):
    __tablename__ = "community_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Post(Base):
    __tablename__ = "community_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    channel_id: Mapped[int] = mapped_column(Integer, ForeignKey("community_channels.id"), nullable=False, index=True)
    author_id: Mapped[int] = mapped_column(Integer, ForeignKey("community_accounts.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)


class Comment(Base):
    """Single-level comments -- no nested replies, keeps the feed cheap to render."""
    __tablename__ = "community_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("community_posts.id"), nullable=False, index=True)
    author_id: Mapped[int] = mapped_column(Integer, ForeignKey("community_accounts.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PostLike(Base):
    """One row per (account, post) -- toggled on/off, gives the Reddit-style like count."""
    __tablename__ = "community_post_likes"
    __table_args__ = (UniqueConstraint("post_id", "account_id", name="uq_community_post_like"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("community_posts.id"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("community_accounts.id"), nullable=False, index=True)


# ============================================================================
# Gifts: admin-created catalog + issued gifts on public profiles.
# ============================================================================

class Gift(Base):
    __tablename__ = "community_gifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    image_url: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GiftInstance(Base):
    __tablename__ = "community_gift_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    gift_id: Mapped[int] = mapped_column(Integer, ForeignKey("community_gifts.id"), nullable=False, index=True)
    recipient_id: Mapped[int] = mapped_column(Integer, ForeignKey("community_accounts.id"), nullable=False, index=True)
    gifted_by: Mapped[str | None] = mapped_column(String(64), default="Адміністрація", nullable=True)
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    gift: Mapped["Gift"] = relationship("Gift")

