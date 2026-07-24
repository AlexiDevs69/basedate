"""
Query functions for the community module -- kept separate from the admin
dashboard's crud.py so the two stay easy to reason about independently.
"""
from datetime import datetime, timedelta, timezone
import re
import secrets

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from community.models import (
    Account,
    Channel,
    Comment,
    CommunityServer,
    DirectMessage,
    DirectThread,
    Friendship,
    Gift,
    GiftInstance,
    Post,
    PostLike,
    ServerChannel,
    ServerBan,
    ServerCategory,
    ServerEvent,
    ServerEventInterest,
    ServerInvite,
    ServerMember,
    ServerMessage,
    UserBlock,
)

# A member counts as "online" if we've seen a request from them in the
# last 3 minutes. Cheap to compute, no background job or websocket needed.
ONLINE_WINDOW = timedelta(minutes=3)

VISUAL_NAME_EFFECTS = {"none", "gradient", "glow"}
VISUAL_NAME_FONTS = {"default", "mono", "serif", "rounded", "cyber", "display", "pixel", "bubble", "puffy", "block", "neon", "glitch", "graffiti", "spooky", "medieval", "roundfat"}

PRESENCE_STATUSES = {"online", "idle", "dnd", "invisible"}
SUPPORTED_LANGUAGES = {"ru", "uk", "en"}
DEFAULT_LANGUAGE = "ru"
SERVER_CHANNEL_TYPES = {"text", "voice", "forum", "announcement", "stage"}
SERVER_EVENT_LOCATION_TYPES = {"stage", "voice", "external"}
SERVER_EVENT_RECURRENCES = {"none", "weekly", "biweekly", "monthly", "yearly", "daily", "weekdays"}


def normalize_language(value: str | None) -> str:
    lang = (value or DEFAULT_LANGUAGE).strip().lower()
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def _normalize_presence_status(value: str | None) -> str:
    status = (value or "online").strip().lower()
    return status if status in PRESENCE_STATUSES else "online"


def _normalize_name_effect(value: str | None) -> str | None:
    effect = (value or "none").strip().lower()
    if effect not in VISUAL_NAME_EFFECTS or effect == "none":
        return None
    return effect


def _normalize_name_font(value: str | None) -> str | None:
    font = (value or "default").strip().lower()
    if font not in VISUAL_NAME_FONTS or font == "default":
        return None
    return font


async def ensure_account_visual_columns(db: AsyncSession) -> None:
    """
    create_all() creates only missing tables; it does not add new columns to
    existing tables. This tiny idempotent helper safely upgrades the existing
    community_accounts table on Render/PostgreSQL without Alembic migrations.
    """
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(512)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS banner_url VARCHAR(512)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS bio TEXT"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS display_name VARCHAR(32)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_effect VARCHAR(32)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_color_start VARCHAR(16)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_color_end VARCHAR(16)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_font VARCHAR(32)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS profile_card_bg_url VARCHAR(512)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS account_status VARCHAR(16) DEFAULT 'online' NOT NULL"))
    await db.execute(text("UPDATE community_accounts SET account_status = 'online' WHERE account_status IS NULL OR account_status = ''"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS language VARCHAR(8) DEFAULT 'ru' NOT NULL"))
    await db.execute(text("UPDATE community_accounts SET language = 'ru' WHERE language IS NULL OR language = ''"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS session_version INTEGER DEFAULT 1 NOT NULL"))
    await db.execute(text("UPDATE community_accounts SET session_version = 1 WHERE session_version IS NULL OR session_version < 1"))
    await db.commit()


_USER_BLOCKS_TABLE_READY = False


async def ensure_user_blocks_table(db: AsyncSession) -> None:
    """Create the directed block table on existing Render databases."""
    global _USER_BLOCKS_TABLE_READY
    if _USER_BLOCKS_TABLE_READY:
        return
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_user_blocks (
            blocker_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            blocked_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            PRIMARY KEY (blocker_id, blocked_id),
            CONSTRAINT ck_community_user_blocks_not_self CHECK (blocker_id <> blocked_id)
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_community_user_blocks_blocked_id "
        "ON community_user_blocks (blocked_id)"
    ))
    await db.commit()
    _USER_BLOCKS_TABLE_READY = True


# Server invite safety guard.
# Render/create_all() does not add columns to old tables, so every route that touches
# invites can call this cheaply. It only does real work once per process.
_SERVER_INVITE_COLUMNS_READY = False

async def ensure_server_invite_columns(db: AsyncSession) -> None:
    global _SERVER_INVITE_COLUMNS_READY
    if _SERVER_INVITE_COLUMNS_READY:
        return
    await db.execute(text("ALTER TABLE community_server_invites ADD COLUMN IF NOT EXISTS code VARCHAR(32)"))
    await db.execute(text("ALTER TABLE community_server_invites ADD COLUMN IF NOT EXISTS is_used BOOLEAN NOT NULL DEFAULT FALSE"))
    await db.execute(text("ALTER TABLE community_server_invites ADD COLUMN IF NOT EXISTS status VARCHAR(16) DEFAULT 'pending' NOT NULL"))
    await db.execute(text("ALTER TABLE community_server_invites ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL"))
    await db.execute(text("ALTER TABLE community_server_invites ADD COLUMN IF NOT EXISTS responded_at TIMESTAMP WITH TIME ZONE"))
    await db.execute(text("UPDATE community_server_invites SET status='pending' WHERE status IS NULL OR status=''"))
    await db.execute(text("UPDATE community_server_invites SET is_used = TRUE WHERE status IN ('accepted','declined')"))
    # Unique random-code index. Postgres permits many NULL values, so old legacy rows
    # without code do not break startup.
    await db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_community_server_invites_code ON community_server_invites (code)"))
    await db.commit()
    _SERVER_INVITE_COLUMNS_READY = True


SERVER_INVITE_DM_PREFIX = "alexihub://server-invite/"


def make_server_invite_dm_content(invite_code: str | int, channel_id: int | None = None) -> str:
    # Keep the visible scheme untouched for the current frontend, but put the
    # random invite code there instead of an incremental id.
    base = f"{SERVER_INVITE_DM_PREFIX}{str(invite_code).strip()}"
    if channel_id:
        return f"{base}?channel={int(channel_id)}"
    return base


def parse_server_invite_dm_content(content: str | None) -> tuple[str, int | None] | None:
    raw = (content or "").strip()
    if not raw.startswith(SERVER_INVITE_DM_PREFIX):
        return None
    m = re.search(r"alexihub://server-invite/([A-Za-z0-9_-]+)(?:\?channel=(\d+))?", raw)
    if not m:
        return None
    invite_code = m.group(1)
    channel_id = int(m.group(2)) if m.group(2) else None
    return invite_code, channel_id


def _new_invite_code() -> str:
    # Numeric on purpose: the existing DM frontend parses invite refs as digits.
    # 12 random digits is still not guessable in practice for this beta.
    return str(secrets.randbelow(900_000_000_000) + 100_000_000_000)


async def _generate_unique_invite_code(db: AsyncSession) -> str:
    await ensure_server_invite_columns(db)
    for _ in range(24):
        code = _new_invite_code()
        result = await db.execute(select(ServerInvite.id).where(ServerInvite.code == code))
        if result.scalar_one_or_none() is None:
            return code
    raise RuntimeError("Could not generate unique server invite code")




async def get_account_by_id(db: AsyncSession, account_id: int) -> Account | None:
    result = await db.execute(select(Account).where(Account.id == account_id))
    return result.scalar_one_or_none()


async def get_account_by_email(db: AsyncSession, email: str) -> Account | None:
    result = await db.execute(select(Account).where(Account.email == email))
    return result.scalar_one_or_none()


async def get_account_by_username(db: AsyncSession, username: str) -> Account | None:
    result = await db.execute(select(Account).where(Account.username == username))
    return result.scalar_one_or_none()


async def get_account_by_username_ci(db: AsyncSession, username: str) -> Account | None:
    result = await db.execute(
        select(Account).where(func.lower(Account.username) == username.strip().lower())
    )
    return result.scalar_one_or_none()


async def get_account_by_telegram_id(db: AsyncSession, telegram_id: int) -> Account | None:
    result = await db.execute(select(Account).where(Account.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def create_account(
    db: AsyncSession,
    username: str,
    email: str | None = None,
    password_hash: str | None = None,
    telegram_id: int | None = None,
    telegram_username: str | None = None,
) -> Account:
    account = Account(
        username=username,
        email=email,
        password_hash=password_hash,
        telegram_id=telegram_id,
        telegram_username=telegram_username,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def update_account_password(
    db: AsyncSession,
    account_id: int,
    password_hash: str,
    *,
    revoke_other_sessions: bool = True,
) -> Account | None:
    await ensure_account_visual_columns(db)
    account = await get_account_by_id(db, account_id)
    if not account:
        return None
    account.password_hash = password_hash
    if revoke_other_sessions:
        account.session_version = max(1, int(getattr(account, "session_version", 1) or 1)) + 1
    await db.commit()
    await db.refresh(account)
    return account


async def update_account_identity(
    db: AsyncSession,
    account_id: int,
    *,
    username: str | None = None,
    email: str | None = None,
) -> Account | None:
    account = await get_account_by_id(db, account_id)
    if not account:
        return None
    if username is not None:
        account.username = username.strip()
    if email is not None:
        account.email = email.strip().lower() or None
    await db.commit()
    await db.refresh(account)
    return account


async def bump_account_session_version(db: AsyncSession, account_id: int) -> Account | None:
    await ensure_account_visual_columns(db)
    account = await get_account_by_id(db, account_id)
    if not account:
        return None
    account.session_version = max(1, int(getattr(account, "session_version", 1) or 1)) + 1
    await db.commit()
    await db.refresh(account)
    return account


async def touch_last_seen(db: AsyncSession, account_id: int) -> None:
    """Cheap presence heartbeat -- called once per request while logged in."""
    account = await get_account_by_id(db, account_id)
    if account:
        account.last_seen_at = datetime.now(timezone.utc)
        if not getattr(account, "account_status", None):
            account.account_status = "online"
        await db.commit()


async def update_presence_status(db: AsyncSession, account_id: int, status: str | None) -> Account | None:
    account = await get_account_by_id(db, account_id)
    if not account:
        return None
    account.account_status = _normalize_presence_status(status)
    account.last_seen_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(account)
    return account


async def update_account_language(db: AsyncSession, account_id: int, language: str | None) -> str | None:
    """Persist the user's UI language.

    Important: use a direct SQL UPDATE instead of mutating an ORM Account object.
    On Render/asyncpg, stale ORM instances after prior commits can make the UI look
    saved while the next refresh still reads the old language. Direct SQL also avoids
    async lazy-load/MissingGreenlet edge cases.
    """
    await ensure_account_visual_columns(db)
    normalized = normalize_language(language)
    result = await db.execute(
        text("UPDATE community_accounts SET language = :language WHERE id = :account_id"),
        {"language": normalized, "account_id": account_id},
    )
    await db.commit()
    if getattr(result, "rowcount", 0) == 0:
        return None
    return normalized


async def list_online_accounts(db: AsyncSession, limit: int = 50) -> list[Account]:
    since = datetime.now(timezone.utc) - ONLINE_WINDOW
    result = await db.execute(
        select(Account)
        .where(Account.last_seen_at >= since, Account.is_banned == False, Account.account_status != "invisible")  # noqa: E712
        .order_by(Account.last_seen_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def update_own_profile(
    db: AsyncSession,
    account_id: int,
    display_name: str | None,
    avatar_url: str | None,
    banner_url: str | None,
    bio: str | None,
) -> Account:
    """
    Self-service edit -- deliberately does NOT touch is_verified/role_label/
    role colors/is_banned. Those are moderation-level fields, set from the
    admin dashboard, not by the account owner.
    """
    account = await get_account_by_id(db, account_id)
    clean_display_name = "".join(
        char for char in (display_name or "").strip()
        if char >= " " and char != "\x7f"
    )[:32].strip()
    account.display_name = (
        clean_display_name
        if clean_display_name and clean_display_name.casefold() != account.username.casefold()
        else None
    )
    account.avatar_url = avatar_url or None
    account.banner_url = banner_url or None
    account.bio = bio or None
    await db.commit()
    await db.refresh(account)
    return account


# ============================================================================
# Friends
# ============================================================================

async def get_friendship_between(db: AsyncSession, a_id: int, b_id: int) -> Friendship | None:
    """One row covers both directions -- (A requested B) and (B requested A)
    are the same relationship."""
    result = await db.execute(
        select(Friendship).where(
            or_(
                and_(Friendship.requester_id == a_id, Friendship.addressee_id == b_id),
                and_(Friendship.requester_id == b_id, Friendship.addressee_id == a_id),
            )
        )
    )
    return result.scalar_one_or_none()


async def friendship_status(db: AsyncSession, viewer_id: int, other_id: int) -> str:
    """One of: 'self', 'none', 'pending_sent', 'pending_received', 'friends'."""
    if viewer_id == other_id:
        return "self"

    fr = await get_friendship_between(db, viewer_id, other_id)
    if fr is None:
        return "none"
    if fr.status == "accepted":
        return "friends"
    if fr.status == "pending":
        return "pending_sent" if fr.requester_id == viewer_id else "pending_received"
    return "none"  # declined -- treat as if there's no relationship, allow re-request


async def send_friend_request(db: AsyncSession, requester_id: int, addressee_id: int) -> Friendship | None:
    if requester_id == addressee_id:
        return None

    existing = await get_friendship_between(db, requester_id, addressee_id)
    if existing and existing.status in ("pending", "accepted"):
        return existing  # already requested/friends -- no duplicate row

    if existing and existing.status == "declined":
        # A previous request was declined -- a fresh request just reopens
        # the same row instead of piling up duplicate history.
        existing.requester_id = requester_id
        existing.addressee_id = addressee_id
        existing.status = "pending"
        existing.responded_at = None
        await db.commit()
        await db.refresh(existing)
        return existing

    friendship = Friendship(requester_id=requester_id, addressee_id=addressee_id, status="pending")
    db.add(friendship)
    await db.commit()
    await db.refresh(friendship)
    return friendship


async def respond_friend_request(
    db: AsyncSession, friendship_id: int, account_id: int, accept: bool
) -> Friendship | None:
    """Only the addressee (the person who RECEIVED the request) may respond to it."""
    result = await db.execute(select(Friendship).where(Friendship.id == friendship_id))
    friendship = result.scalar_one_or_none()

    if not friendship or friendship.addressee_id != account_id or friendship.status != "pending":
        return None

    friendship.status = "accepted" if accept else "declined"
    friendship.responded_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(friendship)
    return friendship


async def remove_friendship(db: AsyncSession, viewer_id: int, other_id: int) -> bool:
    """Un-friending -- fully deletes the row so a future request starts fresh."""
    friendship = await get_friendship_between(db, viewer_id, other_id)
    if not friendship or friendship.status != "accepted":
        return False
    await db.delete(friendship)
    await db.commit()
    return True


# ============================================================================
# Account blocking
# ============================================================================

async def block_account(db: AsyncSession, blocker_id: int, blocked_id: int) -> bool:
    """Block an account and remove any friendship/request between the pair."""
    await ensure_user_blocks_table(db)
    if blocker_id == blocked_id:
        return False
    if not await get_account_by_id(db, blocked_id):
        return False

    await db.execute(
        delete(Friendship).where(
            or_(
                and_(Friendship.requester_id == blocker_id, Friendship.addressee_id == blocked_id),
                and_(Friendship.requester_id == blocked_id, Friendship.addressee_id == blocker_id),
            )
        )
    )
    await db.execute(
        text("""
            INSERT INTO community_user_blocks (blocker_id, blocked_id)
            VALUES (:blocker_id, :blocked_id)
            ON CONFLICT (blocker_id, blocked_id) DO NOTHING
        """),
        {"blocker_id": blocker_id, "blocked_id": blocked_id},
    )
    await db.commit()
    return True


async def unblock_account(db: AsyncSession, blocker_id: int, blocked_id: int) -> bool:
    await ensure_user_blocks_table(db)
    result = await db.execute(
        delete(UserBlock).where(
            UserBlock.blocker_id == blocker_id,
            UserBlock.blocked_id == blocked_id,
        )
    )
    await db.commit()
    return bool(result.rowcount)


async def block_status(db: AsyncSession, viewer_id: int, other_id: int) -> dict[str, bool]:
    await ensure_user_blocks_table(db)
    result = await db.execute(
        select(UserBlock.blocker_id, UserBlock.blocked_id).where(
            or_(
                and_(UserBlock.blocker_id == viewer_id, UserBlock.blocked_id == other_id),
                and_(UserBlock.blocker_id == other_id, UserBlock.blocked_id == viewer_id),
            )
        )
    )
    pairs = {(int(row.blocker_id), int(row.blocked_id)) for row in result.all()}
    return {
        "blocked_by_me": (int(viewer_id), int(other_id)) in pairs,
        "blocked_me": (int(other_id), int(viewer_id)) in pairs,
    }


async def is_blocked_between(db: AsyncSession, account_a_id: int, account_b_id: int) -> bool:
    status = await block_status(db, account_a_id, account_b_id)
    return bool(status["blocked_by_me"] or status["blocked_me"])


async def list_blocked_accounts(db: AsyncSession, blocker_id: int) -> list[tuple[Account, datetime]]:
    await ensure_user_blocks_table(db)
    result = await db.execute(
        select(Account, UserBlock.created_at)
        .join(UserBlock, UserBlock.blocked_id == Account.id)
        .where(UserBlock.blocker_id == blocker_id)
        .order_by(UserBlock.created_at.desc())
    )
    return [(row[0], row[1]) for row in result.all()]


async def list_pending_requests_with_requester(db: AsyncSession, account_id: int) -> list[dict]:
    """Pending incoming requests, each paired with the requester's Account -- used for the bell dropdown."""
    result = await db.execute(
        select(Friendship)
        .where(Friendship.addressee_id == account_id, Friendship.status == "pending")
        .order_by(Friendship.created_at.desc())
    )
    pending = list(result.scalars().all())

    items = []
    for fr in pending:
        requester = await get_account_by_id(db, fr.requester_id)
        items.append({"friendship_id": fr.id, "requester": requester, "created_at": fr.created_at})
    return items



async def list_pending_sent_with_addressee(db: AsyncSession, account_id: int) -> list[dict]:
    """Pending outgoing requests, each paired with the addressee Account -- used on the friends page."""
    result = await db.execute(
        select(Friendship)
        .where(Friendship.requester_id == account_id, Friendship.status == "pending")
        .order_by(Friendship.created_at.desc())
    )
    pending = list(result.scalars().all())

    items = []
    for fr in pending:
        addressee = await get_account_by_id(db, fr.addressee_id)
        items.append({"friendship_id": fr.id, "addressee": addressee, "created_at": fr.created_at})
    return items


async def cancel_pending_friend_request(db: AsyncSession, friendship_id: int, requester_id: int) -> bool:
    """Requester can cancel an outgoing pending friend request."""
    result = await db.execute(select(Friendship).where(Friendship.id == friendship_id))
    friendship = result.scalar_one_or_none()
    if not friendship or friendship.requester_id != requester_id or friendship.status != "pending":
        return False
    await db.delete(friendship)
    await db.commit()
    return True


async def list_friends(db: AsyncSession, account_id: int) -> list[Account]:
    result = await db.execute(
        select(Friendship).where(
            or_(Friendship.requester_id == account_id, Friendship.addressee_id == account_id),
            Friendship.status == "accepted",
        )
    )
    friendships = result.scalars().all()
    other_ids = [
        fr.addressee_id if fr.requester_id == account_id else fr.requester_id for fr in friendships
    ]
    if not other_ids:
        return []

    result = await db.execute(select(Account).where(Account.id.in_(other_ids)))
    return list(result.scalars().all())


# ============================================================================
# Admin moderation (called from the admin dashboard's routes in main.py)
# ============================================================================

PAGE_SIZE = 50


async def get_accounts_page(db: AsyncSession, page: int = 1) -> tuple[list[Account], int]:
    offset = (page - 1) * PAGE_SIZE
    total_result = await db.execute(select(func.count()).select_from(Account))
    total = total_result.scalar_one()
    result = await db.execute(
        select(Account).order_by(Account.created_at.desc()).limit(PAGE_SIZE).offset(offset)
    )
    return list(result.scalars().all()), total


async def update_account_moderation(
    db: AsyncSession,
    account_id: int,
    is_verified: bool,
    role_label: str | None,
    role_color_start: str | None,
    role_color_end: str | None,
    is_banned: bool,
    name_effect: str | None = None,
    name_color_start: str | None = None,
    name_color_end: str | None = None,
    name_font: str | None = None,
    profile_card_bg_url: str | None = None,
) -> Account | None:
    account = await get_account_by_id(db, account_id)
    if not account:
        return None

    account.is_verified = is_verified
    account.role_label = role_label or None
    account.role_color_start = role_color_start or None
    account.role_color_end = role_color_end or None
    account.is_banned = is_banned

    account.name_effect = _normalize_name_effect(name_effect)
    account.name_color_start = name_color_start or None
    account.name_color_end = name_color_end or None
    account.name_font = _normalize_name_font(name_font)
    account.profile_card_bg_url = profile_card_bg_url or None

    await db.commit()
    await db.refresh(account)
    return account


async def delete_account(db: AsyncSession, account_id: int) -> bool:
    account = await get_account_by_id(db, account_id)
    if not account:
        return False

    # If the account owns servers, delete those servers and their content first.
    owned_servers_result = await db.execute(
        select(CommunityServer).where(CommunityServer.owner_id == account_id)
    )
    for server in owned_servers_result.scalars().all():
        await delete_server(db, server.id)

    # Server relations where this account is not the owner.
    for model, clauses in [
        (ServerMessage, [ServerMessage.author_id == account_id]),
        (ServerInvite, [or_(ServerInvite.inviter_id == account_id, ServerInvite.invitee_id == account_id)]),
        (ServerMember, [ServerMember.account_id == account_id]),
    ]:
        result = await db.execute(select(model).where(*clauses))
        for row in result.scalars().all():
            await db.delete(row)

    # Remove issued gifts first so the gift foreign key never blocks deleting
    # a community account from the admin panel.
    gift_instances_result = await db.execute(
        select(GiftInstance).where(GiftInstance.recipient_id == account_id)
    )
    for gift_instance in gift_instances_result.scalars().all():
        await db.delete(gift_instance)

    await db.delete(account)
    await db.commit()
    return True


# ============================================================================
# Forum
# ============================================================================

DEFAULT_CHANNELS = [
    ("general", "Загальне", "Про все на світі", 0),
    ("help", "Допомога", "Питання і підтримка", 1),
    ("offtopic", "Флуд", "Все, що не по темі", 2),
]


async def ensure_default_channels(db: AsyncSession) -> None:
    """Idempotent -- only seeds channels the very first time the forum is used."""
    result = await db.execute(select(Channel))
    if result.scalars().first() is not None:
        return
    for slug, name, description, order in DEFAULT_CHANNELS:
        db.add(Channel(slug=slug, name=name, description=description, sort_order=order))
    await db.commit()


async def list_channels(db: AsyncSession) -> list[Channel]:
    result = await db.execute(select(Channel).order_by(Channel.sort_order))
    return list(result.scalars().all())


async def get_channel_by_slug(db: AsyncSession, slug: str) -> Channel | None:
    result = await db.execute(select(Channel).where(Channel.slug == slug))
    return result.scalar_one_or_none()


async def create_post(
    db: AsyncSession, channel_id: int, author_id: int, content: str, image_url: str | None = None
) -> Post:
    post = Post(channel_id=channel_id, author_id=author_id, content=content, image_url=image_url or None)
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return post


async def list_posts_for_channel(db: AsyncSession, channel_id: int, limit: int = 50) -> list[Post]:
    result = await db.execute(
        select(Post).where(Post.channel_id == channel_id).order_by(Post.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def toggle_like(db: AsyncSession, post_id: int, account_id: int) -> bool:
    """Returns True if the post is now liked, False if the like was just removed."""
    result = await db.execute(
        select(PostLike).where(PostLike.post_id == post_id, PostLike.account_id == account_id)
    )
    like = result.scalar_one_or_none()
    if like:
        await db.delete(like)
        await db.commit()
        return False
    db.add(PostLike(post_id=post_id, account_id=account_id))
    await db.commit()
    return True


async def count_likes(db: AsyncSession, post_id: int) -> int:
    result = await db.execute(select(func.count()).select_from(PostLike).where(PostLike.post_id == post_id))
    return result.scalar_one()


async def has_liked(db: AsyncSession, post_id: int, account_id: int) -> bool:
    result = await db.execute(
        select(PostLike).where(PostLike.post_id == post_id, PostLike.account_id == account_id)
    )
    return result.scalar_one_or_none() is not None


async def add_comment(db: AsyncSession, post_id: int, author_id: int, content: str) -> Comment:
    comment = Comment(post_id=post_id, author_id=author_id, content=content)
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return comment


async def list_comments_for_post(db: AsyncSession, post_id: int) -> list[Comment]:
    result = await db.execute(
        select(Comment).where(Comment.post_id == post_id).order_by(Comment.created_at.asc())
    )
    return list(result.scalars().all())


async def get_channel_feed(db: AsyncSession, channel_id: int, viewer_id: int | None, limit: int = 50) -> list[dict]:
    """
    Assembles everything a channel page needs to render: each post bundled
    with its author, like count/state, and its (flat) comments with their
    authors. Simple sequential queries -- totally fine at this scale, and
    much easier to read than a hand-joined mega-query.
    """
    posts = await list_posts_for_channel(db, channel_id, limit=limit)
    feed = []
    for post in posts:
        author = await get_account_by_id(db, post.author_id)
        like_count = await count_likes(db, post.id)
        liked = await has_liked(db, post.id, viewer_id) if viewer_id else False

        comments = []
        for comment in await list_comments_for_post(db, post.id):
            comment_author = await get_account_by_id(db, comment.author_id)
            comments.append({"comment": comment, "author": comment_author})

        feed.append({
            "post": post,
            "author": author,
            "like_count": like_count,
            "liked": liked,
            "comments": comments,
        })
    return feed


# ============================================================================
# User-created servers
# ============================================================================

async def list_servers_for_account(db: AsyncSession, account_id: int) -> list[CommunityServer]:
    result = await db.execute(
        select(CommunityServer)
        .join(ServerMember, ServerMember.server_id == CommunityServer.id)
        .where(ServerMember.account_id == account_id)
        .order_by(CommunityServer.created_at.asc())
    )
    return list(result.scalars().all())


async def list_mutual_servers(
    db: AsyncSession,
    first_account_id: int,
    second_account_id: int,
) -> list[CommunityServer]:
    """Return servers where both accounts are current members."""
    first_membership = aliased(ServerMember)
    second_membership = aliased(ServerMember)
    result = await db.execute(
        select(CommunityServer)
        .join(first_membership, first_membership.server_id == CommunityServer.id)
        .join(second_membership, second_membership.server_id == CommunityServer.id)
        .where(
            first_membership.account_id == first_account_id,
            second_membership.account_id == second_account_id,
        )
        .order_by(CommunityServer.name.asc(), CommunityServer.id.asc())
    )
    return list(result.scalars().all())


async def get_server_by_id(db: AsyncSession, server_id: int) -> CommunityServer | None:
    result = await db.execute(select(CommunityServer).where(CommunityServer.id == server_id))
    return result.scalar_one_or_none()


async def get_server_member(db: AsyncSession, server_id: int, account_id: int) -> ServerMember | None:
    result = await db.execute(
        select(ServerMember).where(ServerMember.server_id == server_id, ServerMember.account_id == account_id)
    )
    return result.scalar_one_or_none()


_SERVER_MODERATION_TABLES_READY = False


async def ensure_server_moderation_tables(db: AsyncSession) -> None:
    """Create the server-ban storage on existing Render/PostgreSQL databases."""
    global _SERVER_MODERATION_TABLES_READY
    if _SERVER_MODERATION_TABLES_READY:
        return
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_server_bans (
            id SERIAL PRIMARY KEY,
            server_id INTEGER NOT NULL REFERENCES community_servers(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            banned_by_id INTEGER REFERENCES community_accounts(id) ON DELETE SET NULL,
            reason VARCHAR(255),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_community_server_ban "
        "ON community_server_bans (server_id, account_id)"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_community_server_bans_server_created "
        "ON community_server_bans (server_id, created_at DESC)"
    ))
    await db.commit()
    _SERVER_MODERATION_TABLES_READY = True


_SERVER_CATEGORY_SCHEMA_READY = False


async def ensure_server_category_schema(db: AsyncSession) -> None:
    """Add channel categories without invalidating existing Render databases."""
    global _SERVER_CATEGORY_SCHEMA_READY
    if _SERVER_CATEGORY_SCHEMA_READY:
        return
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_server_categories (
            id SERIAL PRIMARY KEY,
            server_id INTEGER NOT NULL REFERENCES community_servers(id) ON DELETE CASCADE,
            name VARCHAR(64) NOT NULL,
            is_private BOOLEAN NOT NULL DEFAULT FALSE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_community_server_categories_server_id "
        "ON community_server_categories (server_id)"
    ))
    await db.execute(text(
        "ALTER TABLE community_server_channels "
        "ADD COLUMN IF NOT EXISTS category_id INTEGER"
    ))
    await db.execute(text(
        "ALTER TABLE community_server_channels "
        "ADD COLUMN IF NOT EXISTS channel_type VARCHAR(16) NOT NULL DEFAULT 'text'"
    ))
    await db.execute(text(
        "ALTER TABLE community_server_channels "
        "ADD COLUMN IF NOT EXISTS is_private BOOLEAN NOT NULL DEFAULT FALSE"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_community_server_channels_category_id "
        "ON community_server_channels (category_id)"
    ))
    await db.execute(text(
        "UPDATE community_server_channels SET channel_type = 'text' "
        "WHERE channel_type IS NULL OR channel_type = ''"
    ))
    await db.commit()
    _SERVER_CATEGORY_SCHEMA_READY = True


_SERVER_EVENT_SCHEMA_READY = False


async def ensure_server_event_schema(db: AsyncSession) -> None:
    """Create event and interested tables on existing Render/PostgreSQL databases."""
    global _SERVER_EVENT_SCHEMA_READY
    if _SERVER_EVENT_SCHEMA_READY:
        return
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_server_events (
            id SERIAL PRIMARY KEY,
            server_id INTEGER NOT NULL REFERENCES community_servers(id) ON DELETE CASCADE,
            creator_id INTEGER REFERENCES community_accounts(id) ON DELETE SET NULL,
            title VARCHAR(100) NOT NULL,
            description TEXT,
            location_type VARCHAR(16) NOT NULL DEFAULT 'external',
            location VARCHAR(255) NOT NULL,
            cover_url VARCHAR(512),
            start_at TIMESTAMP WITH TIME ZONE NOT NULL,
            end_at TIMESTAMP WITH TIME ZONE NOT NULL,
            recurrence VARCHAR(16) NOT NULL DEFAULT 'none',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_server_event_interests (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES community_server_events(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_community_server_events_server_start "
        "ON community_server_events (server_id, start_at)"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_community_server_event_interests_event "
        "ON community_server_event_interests (event_id)"
    ))
    await db.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_community_server_event_interest "
        "ON community_server_event_interests (event_id, account_id)"
    ))
    await db.commit()
    _SERVER_EVENT_SCHEMA_READY = True


async def is_server_banned(db: AsyncSession, server_id: int, account_id: int) -> bool:
    await ensure_server_moderation_tables(db)
    result = await db.execute(
        select(ServerBan.id).where(
            ServerBan.server_id == int(server_id),
            ServerBan.account_id == int(account_id),
        )
    )
    return result.scalar_one_or_none() is not None


async def is_server_member(db: AsyncSession, server_id: int, account_id: int) -> bool:
    return await get_server_member(db, server_id, account_id) is not None


async def join_server_by_id(db: AsyncSession, server_id: int, account_id: int) -> CommunityServer | None:
    """Deprecated unsafe MVP helper.

    Direct joining by server id is intentionally disabled to remove the IDOR
    class where a user could guess / paste a server id and join or probe it.
    Use accept_server_invite_by_code() instead.
    """
    return None


async def accept_server_invite_by_code(db: AsyncSession, invite_code: str | int, account_id: int) -> ServerInvite | None:
    invite = await get_server_invite_by_ref(db, str(invite_code))
    if not invite:
        return None
    if int(invite.invitee_id) != int(account_id):
        return None
    if invite.status != "pending" or bool(getattr(invite, "is_used", False)):
        return None

    server_id = int(invite.server_id)
    if await is_server_banned(db, server_id, account_id):
        return None
    invite.status = "accepted"
    invite.is_used = True
    invite.responded_at = datetime.now(timezone.utc)

    if not await is_server_member(db, server_id, account_id):
        db.add(ServerMember(server_id=server_id, account_id=account_id, role="member"))

    await db.commit()
    await db.refresh(invite)
    return invite


async def can_manage_server(db: AsyncSession, server_id: int, account_id: int) -> bool:
    server = await get_server_by_id(db, server_id)
    if server and int(server.owner_id) == int(account_id):
        return True
    member = await get_server_member(db, server_id, account_id)
    return bool(member and member.role == "admin")


async def create_server(
    db: AsyncSession,
    owner_id: int,
    name: str,
    icon_url: str | None = None,
    description: str | None = None,
) -> CommunityServer:
    server = CommunityServer(
        owner_id=owner_id,
        name=name.strip()[:64],
        icon_url=icon_url.strip() if icon_url else None,
        description=description.strip()[:255] if description else None,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)

    db.add(ServerMember(server_id=server.id, account_id=owner_id, role="owner"))
    db.add(ServerChannel(server_id=server.id, name="general", description="Основний чат", sort_order=0))
    await db.commit()
    return server


async def update_server_settings(
    db: AsyncSession,
    server_id: int,
    name: str,
    icon_url: str | None = None,
    description: str | None = None,
) -> CommunityServer | None:
    server = await get_server_by_id(db, server_id)
    if not server:
        return None

    clean_name = name.strip()[:64]
    if clean_name:
        server.name = clean_name
    server.icon_url = icon_url.strip()[:512] if icon_url and icon_url.strip() else None
    server.description = description.strip()[:255] if description and description.strip() else None
    await db.commit()
    await db.refresh(server)
    return server


async def leave_server(db: AsyncSession, server_id: int, account_id: int) -> bool:
    member = await get_server_member(db, server_id, account_id)
    if not member:
        return False

    server = await get_server_by_id(db, server_id)
    if server and server.owner_id == account_id:
        return False

    await db.delete(member)
    await db.commit()
    return True


async def delete_server(db: AsyncSession, server_id: int) -> bool:
    await ensure_server_moderation_tables(db)
    await ensure_server_category_schema(db)
    await ensure_server_event_schema(db)
    server = await get_server_by_id(db, server_id)
    if not server:
        return False

    event_result = await db.execute(
        select(ServerEvent.id).where(ServerEvent.server_id == server_id)
    )
    event_ids = list(event_result.scalars().all())
    if event_ids:
        interests = await db.execute(
            select(ServerEventInterest).where(ServerEventInterest.event_id.in_(event_ids))
        )
        for row in interests.scalars().all():
            await db.delete(row)

    for model, column in [
        (ServerMessage, ServerMessage.server_id),
        (ServerInvite, ServerInvite.server_id),
        (ServerBan, ServerBan.server_id),
        (ServerMember, ServerMember.server_id),
        (ServerChannel, ServerChannel.server_id),
        (ServerCategory, ServerCategory.server_id),
        (ServerEvent, ServerEvent.server_id),
    ]:
        result = await db.execute(select(model).where(column == server_id))
        for row in result.scalars().all():
            await db.delete(row)

    await db.delete(server)
    await db.commit()
    return True


async def list_server_categories(
    db: AsyncSession,
    server_id: int,
    include_private: bool = True,
) -> list[ServerCategory]:
    await ensure_server_category_schema(db)
    query = select(ServerCategory).where(ServerCategory.server_id == server_id)
    if not include_private:
        query = query.where(ServerCategory.is_private.is_(False))
    result = await db.execute(query.order_by(ServerCategory.sort_order.asc(), ServerCategory.id.asc()))
    return list(result.scalars().all())


async def get_server_category(
    db: AsyncSession,
    server_id: int,
    category_id: int,
) -> ServerCategory | None:
    await ensure_server_category_schema(db)
    result = await db.execute(
        select(ServerCategory).where(
            ServerCategory.server_id == server_id,
            ServerCategory.id == category_id,
        )
    )
    return result.scalar_one_or_none()


async def create_server_category(
    db: AsyncSession,
    server_id: int,
    name: str,
    is_private: bool = False,
) -> ServerCategory:
    await ensure_server_category_schema(db)
    clean_name = re.sub(r"\s+", " ", name.strip())[:64] or "Новая категория"
    count_result = await db.execute(
        select(func.count()).select_from(ServerCategory).where(ServerCategory.server_id == server_id)
    )
    category = ServerCategory(
        server_id=server_id,
        name=clean_name,
        is_private=bool(is_private),
        sort_order=int(count_result.scalar_one() or 0),
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return category


async def create_server_event(
    db: AsyncSession,
    server_id: int,
    creator_id: int,
    title: str,
    description: str | None,
    location_type: str,
    location: str,
    cover_url: str | None,
    start_at: datetime,
    end_at: datetime,
    recurrence: str = "none",
) -> ServerEvent:
    await ensure_server_event_schema(db)
    clean_location_type = (location_type or "external").strip().lower()
    if clean_location_type not in SERVER_EVENT_LOCATION_TYPES:
        clean_location_type = "external"
    clean_recurrence = (recurrence or "none").strip().lower()
    if clean_recurrence not in SERVER_EVENT_RECURRENCES:
        clean_recurrence = "none"
    event = ServerEvent(
        server_id=server_id,
        creator_id=creator_id,
        title=re.sub(r"\s+", " ", title.strip())[:100],
        description=(description or "").strip()[:4000] or None,
        location_type=clean_location_type,
        location=re.sub(r"\s+", " ", (location or "").strip())[:255],
        cover_url=(cover_url or "").strip()[:512] or None,
        start_at=start_at,
        end_at=end_at,
        recurrence=clean_recurrence,
    )
    db.add(event)
    await db.flush()
    db.add(ServerEventInterest(event_id=event.id, account_id=creator_id))
    await db.commit()
    await db.refresh(event)
    return event


async def get_server_event(
    db: AsyncSession,
    server_id: int,
    event_id: int,
) -> ServerEvent | None:
    await ensure_server_event_schema(db)
    result = await db.execute(
        select(ServerEvent).where(
            ServerEvent.server_id == int(server_id),
            ServerEvent.id == int(event_id),
        )
    )
    return result.scalar_one_or_none()


async def _server_event_payload(
    db: AsyncSession,
    event: ServerEvent,
    viewer_id: int,
) -> dict:
    creator = None
    if event.creator_id:
        creator_result = await db.execute(select(Account).where(Account.id == event.creator_id))
        creator = creator_result.scalar_one_or_none()

    interest_result = await db.execute(
        select(ServerEventInterest, Account)
        .join(Account, Account.id == ServerEventInterest.account_id)
        .where(ServerEventInterest.event_id == event.id)
        .order_by(ServerEventInterest.created_at.asc())
    )
    interested_rows = interest_result.all()
    interested_accounts = [
        {
            "id": int(account.id),
            "username": account.username,
            "display_name": account.display_name or account.username,
            "avatar_url": account.avatar_url or "",
        }
        for _, account in interested_rows
    ]
    return _compose_server_event_payload(event, creator, interested_accounts, viewer_id)


def _compose_server_event_payload(
    event: ServerEvent,
    creator: Account | None,
    interested_accounts: list[dict],
    viewer_id: int,
) -> dict:
    return {
        "id": int(event.id),
        "server_id": int(event.server_id),
        "title": event.title,
        "description": event.description or "",
        "location_type": event.location_type,
        "location": event.location,
        "cover_url": event.cover_url or "",
        "start_at": event.start_at.isoformat(),
        "end_at": event.end_at.isoformat(),
        "recurrence": event.recurrence,
        "creator": {
            "id": int(creator.id),
            "username": creator.username,
            "display_name": creator.display_name or creator.username,
            "avatar_url": creator.avatar_url or "",
        } if creator else None,
        "interest_count": len(interested_accounts),
        "viewer_interested": any(item["id"] == int(viewer_id) for item in interested_accounts),
        "interested": interested_accounts,
    }


async def get_server_event_payload(
    db: AsyncSession,
    server_id: int,
    event_id: int,
    viewer_id: int,
) -> dict | None:
    event = await get_server_event(db, server_id, event_id)
    if not event:
        return None
    return await _server_event_payload(db, event, viewer_id)


async def list_server_events(
    db: AsyncSession,
    server_id: int,
    viewer_id: int,
) -> list[dict]:
    await ensure_server_event_schema(db)
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ServerEvent)
        .where(
            ServerEvent.server_id == int(server_id),
            or_(
                ServerEvent.end_at > now,
                ServerEvent.recurrence != "none",
            ),
        )
        .order_by(ServerEvent.start_at.asc(), ServerEvent.id.asc())
        .limit(100)
    )
    events = list(result.scalars().all())
    if not events:
        return []

    creator_ids = {int(event.creator_id) for event in events if event.creator_id}
    creators: dict[int, Account] = {}
    if creator_ids:
        creator_result = await db.execute(select(Account).where(Account.id.in_(creator_ids)))
        creators = {int(account.id): account for account in creator_result.scalars().all()}

    event_ids = [int(event.id) for event in events]
    interest_result = await db.execute(
        select(ServerEventInterest, Account)
        .join(Account, Account.id == ServerEventInterest.account_id)
        .where(ServerEventInterest.event_id.in_(event_ids))
        .order_by(ServerEventInterest.created_at.asc())
    )
    interested_by_event: dict[int, list[dict]] = {event_id: [] for event_id in event_ids}
    for interest, account in interest_result.all():
        interested_by_event.setdefault(int(interest.event_id), []).append({
            "id": int(account.id),
            "username": account.username,
            "display_name": account.display_name or account.username,
            "avatar_url": account.avatar_url or "",
        })

    return [
        _compose_server_event_payload(
            event,
            creators.get(int(event.creator_id)) if event.creator_id else None,
            interested_by_event.get(int(event.id), []),
            viewer_id,
        )
        for event in events
    ]


async def toggle_server_event_interest(
    db: AsyncSession,
    server_id: int,
    event_id: int,
    account_id: int,
) -> dict | None:
    event = await get_server_event(db, server_id, event_id)
    if not event:
        return None
    result = await db.execute(
        select(ServerEventInterest).where(
            ServerEventInterest.event_id == int(event_id),
            ServerEventInterest.account_id == int(account_id),
        )
    )
    existing = result.scalar_one_or_none()
    interested = existing is None
    if existing:
        await db.delete(existing)
    else:
        db.add(ServerEventInterest(event_id=event_id, account_id=account_id))
    await db.commit()
    payload = await _server_event_payload(db, event, account_id)
    return {
        "interested": interested,
        "count": payload["interest_count"],
        "accounts": payload["interested"],
    }


async def delete_server_event(
    db: AsyncSession,
    server_id: int,
    event_id: int,
) -> bool:
    event = await get_server_event(db, server_id, event_id)
    if not event:
        return False
    await db.delete(event)
    await db.commit()
    return True


async def end_server_event(
    db: AsyncSession,
    server_id: int,
    event_id: int,
) -> ServerEvent | None:
    event = await get_server_event(db, server_id, event_id)
    if not event:
        return None
    event.end_at = datetime.now(timezone.utc)
    event.recurrence = "none"
    await db.commit()
    await db.refresh(event)
    return event


async def list_server_channels(
    db: AsyncSession,
    server_id: int,
    include_private: bool = True,
) -> list[ServerChannel]:
    await ensure_server_category_schema(db)
    query = select(ServerChannel).where(ServerChannel.server_id == server_id)
    if not include_private:
        query = (
            query.outerjoin(ServerCategory, ServerCategory.id == ServerChannel.category_id)
            .where(
                ServerChannel.is_private.is_(False),
                or_(ServerChannel.category_id.is_(None), ServerCategory.is_private.is_(False)),
            )
        )
    result = await db.execute(
        query.order_by(ServerChannel.sort_order.asc(), ServerChannel.id.asc())
    )
    return list(result.scalars().all())


async def get_server_channel(db: AsyncSession, server_id: int, channel_id: int) -> ServerChannel | None:
    await ensure_server_category_schema(db)
    result = await db.execute(
        select(ServerChannel).where(ServerChannel.server_id == server_id, ServerChannel.id == channel_id)
    )
    return result.scalar_one_or_none()


async def can_access_server_channel(
    db: AsyncSession,
    server_id: int,
    channel: ServerChannel,
    account_id: int,
) -> bool:
    if await can_manage_server(db, server_id, account_id):
        return True
    if bool(channel.is_private):
        return False
    if channel.category_id:
        category = await get_server_category(db, server_id, int(channel.category_id))
        if category and bool(category.is_private):
            return False
    return True


async def create_server_channel(
    db: AsyncSession,
    server_id: int,
    name: str,
    description: str | None = None,
    category_id: int | None = None,
    channel_type: str = "text",
    is_private: bool = False,
) -> ServerChannel:
    await ensure_server_category_schema(db)
    clean_name = name.strip().lower().replace(" ", "-")[:64]
    if not clean_name:
        clean_name = "new-channel"
    clean_type = (channel_type or "text").strip().lower()
    if clean_type not in SERVER_CHANNEL_TYPES:
        clean_type = "text"
    valid_category_id: int | None = None
    if category_id is not None:
        category = await get_server_category(db, server_id, int(category_id))
        if category:
            valid_category_id = int(category.id)
    count_query = select(func.count()).select_from(ServerChannel).where(ServerChannel.server_id == server_id)
    if valid_category_id is None:
        count_query = count_query.where(ServerChannel.category_id.is_(None))
    else:
        count_query = count_query.where(ServerChannel.category_id == valid_category_id)
    count_result = await db.execute(count_query)
    order = int(count_result.scalar_one() or 0)
    channel = ServerChannel(
        server_id=server_id,
        category_id=valid_category_id,
        name=clean_name,
        description=description.strip()[:255] if description else None,
        channel_type=clean_type,
        is_private=bool(is_private),
        sort_order=order,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return channel


async def update_server_channel(
    db: AsyncSession,
    server_id: int,
    channel_id: int,
    name: str,
    description: str | None = None,
) -> ServerChannel | None:
    channel = await get_server_channel(db, server_id, channel_id)
    if not channel:
        return None
    clean_name = name.strip().lower().replace(" ", "-")[:64]
    if not clean_name:
        clean_name = channel.name
    channel.name = clean_name
    channel.description = description.strip()[:255] if description else None
    await db.commit()
    await db.refresh(channel)
    return channel


async def delete_server_channel(db: AsyncSession, server_id: int, channel_id: int) -> bool:
    channel = await get_server_channel(db, server_id, channel_id)
    if not channel:
        return False
    count_result = await db.execute(select(func.count()).select_from(ServerChannel).where(ServerChannel.server_id == server_id))
    channel_count = int(count_result.scalar_one() or 0)
    if channel_count <= 1:
        return False
    messages = await db.execute(select(ServerMessage).where(ServerMessage.server_id == server_id, ServerMessage.channel_id == channel_id))
    for msg in messages.scalars().all():
        await db.delete(msg)
    await db.delete(channel)
    await db.commit()
    return True


async def create_server_message(
    db: AsyncSession,
    server_id: int,
    channel_id: int,
    author_id: int,
    content: str,
    image_url: str | None = None,
    reply_to_id: int | None = None,
    is_forwarded: bool = False,
) -> ServerMessage:
    await ensure_message_meta_columns(db)
    message = ServerMessage(
        server_id=server_id,
        channel_id=channel_id,
        author_id=author_id,
        reply_to_id=reply_to_id,
        is_forwarded=bool(is_forwarded),
        content=content.strip(),
        image_url=image_url.strip() if image_url else None,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


async def get_server_message(
    db: AsyncSession,
    server_id: int,
    channel_id: int,
    message_id: int,
) -> ServerMessage | None:
    await ensure_message_meta_columns(db)
    result = await db.execute(
        select(ServerMessage).where(
            ServerMessage.id == message_id,
            ServerMessage.server_id == server_id,
            ServerMessage.channel_id == channel_id,
        )
    )
    return result.scalar_one_or_none()


async def get_server_message_by_id(db: AsyncSession, message_id: int) -> ServerMessage | None:
    """Return a server message by id. Authorization is checked in the router."""
    await ensure_message_meta_columns(db)
    result = await db.execute(select(ServerMessage).where(ServerMessage.id == message_id))
    return result.scalar_one_or_none()


async def update_server_message(
    db: AsyncSession,
    message: ServerMessage,
    content: str,
    image_url: str | None = None,
) -> ServerMessage:
    await ensure_message_meta_columns(db)
    if getattr(message, "is_forwarded", False):
        return message
    message.content = content.strip()
    message.image_url = image_url.strip() if image_url and image_url.strip() else None
    message.edited_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(message)
    return message


async def delete_server_message(db: AsyncSession, message: ServerMessage) -> None:
    await db.delete(message)
    await db.commit()


async def list_server_messages(db: AsyncSession, server_id: int, channel_id: int, limit: int = 80) -> list[ServerMessage]:
    await ensure_message_meta_columns(db)
    result = await db.execute(
        select(ServerMessage)
        .where(ServerMessage.server_id == server_id, ServerMessage.channel_id == channel_id)
        .order_by(ServerMessage.created_at.desc())
        .limit(limit)
    )
    return list(reversed(list(result.scalars().all())))


async def list_server_messages_after(
    db: AsyncSession,
    server_id: int,
    channel_id: int,
    after_id: int,
    limit: int = 201,
) -> list[ServerMessage]:
    """Return messages missed by a temporarily disconnected channel client."""
    await ensure_message_meta_columns(db)
    safe_limit = max(1, min(int(limit or 201), 501))
    result = await db.execute(
        select(ServerMessage)
        .where(
            ServerMessage.server_id == int(server_id),
            ServerMessage.channel_id == int(channel_id),
            ServerMessage.id > max(0, int(after_id or 0)),
        )
        .order_by(ServerMessage.id.asc())
        .limit(safe_limit)
    )
    return list(result.scalars().all())


async def get_server_feed(db: AsyncSession, server_id: int, channel_id: int, limit: int = 80) -> list[dict]:
    messages = await list_server_messages(db, server_id, channel_id, limit=limit)
    feed = []
    for msg in messages:
        author = await get_account_by_id(db, msg.author_id)
        reply = None
        reply_id = getattr(msg, "reply_to_id", None)
        if reply_id:
            reply_msg = await get_server_message(db, server_id, channel_id, int(reply_id))
            if reply_msg:
                reply_author = await get_account_by_id(db, reply_msg.author_id)
                reply = {"message": reply_msg, "author": reply_author}
        feed.append({"message": msg, "author": author, "reply": reply})
    return feed


async def list_server_members(db: AsyncSession, server_id: int) -> list[dict]:
    result = await db.execute(
        select(ServerMember).where(ServerMember.server_id == server_id).order_by(ServerMember.joined_at.asc())
    )
    members = []
    for member in result.scalars().all():
        account = await get_account_by_id(db, member.account_id)
        members.append({"member": member, "account": account})
    role_rank = {"owner": 0, "admin": 1, "member": 2}
    members.sort(key=lambda item: (
        role_rank.get(item["member"].role, 9),
        item["member"].joined_at.isoformat() if item["member"].joined_at else "",
    ))
    return members


async def list_server_bans(db: AsyncSession, server_id: int) -> list[dict]:
    await ensure_server_moderation_tables(db)
    result = await db.execute(
        select(ServerBan)
        .where(ServerBan.server_id == int(server_id))
        .order_by(ServerBan.created_at.desc())
    )
    bans = []
    for ban in result.scalars().all():
        bans.append({
            "ban": ban,
            "account": await get_account_by_id(db, ban.account_id),
            "banned_by": await get_account_by_id(db, ban.banned_by_id) if ban.banned_by_id else None,
        })
    return bans


def _can_act_on_member(server: CommunityServer, actor: ServerMember | None, target: ServerMember) -> bool:
    if not actor or int(actor.account_id) == int(target.account_id):
        return False
    if int(server.owner_id) == int(target.account_id):
        return False
    if int(server.owner_id) == int(actor.account_id):
        return True
    return actor.role == "admin" and target.role == "member"


async def _get_server_for_moderation(db: AsyncSession, server_id: int) -> CommunityServer | None:
    result = await db.execute(
        select(CommunityServer)
        .where(CommunityServer.id == int(server_id))
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def update_server_member_role(
    db: AsyncSession,
    server_id: int,
    actor_id: int,
    target_id: int,
    role: str,
) -> tuple[bool, str]:
    clean_role = (role or "").strip().lower()
    if clean_role not in {"admin", "member"}:
        return False, "invalid_role"
    server = await _get_server_for_moderation(db, server_id)
    actor = await get_server_member(db, server_id, actor_id)
    target = await get_server_member(db, server_id, target_id)
    if not server or not actor or not target:
        return False, "member_not_found"
    if int(server.owner_id) != int(actor_id):
        return False, "owner_only"
    if int(target_id) in {int(actor_id), int(server.owner_id)}:
        return False, "owner_role_locked"
    target.role = clean_role
    await db.commit()
    await db.refresh(target)
    return True, clean_role


async def kick_server_member(
    db: AsyncSession,
    server_id: int,
    actor_id: int,
    target_id: int,
) -> tuple[bool, str]:
    server = await _get_server_for_moderation(db, server_id)
    actor = await get_server_member(db, server_id, actor_id)
    target = await get_server_member(db, server_id, target_id)
    if not server or not target:
        return False, "member_not_found"
    if not _can_act_on_member(server, actor, target):
        return False, "forbidden"
    pending = await db.execute(
        select(ServerInvite).where(
            ServerInvite.server_id == int(server_id),
            ServerInvite.invitee_id == int(target_id),
            ServerInvite.status == "pending",
        )
    )
    for invite in pending.scalars().all():
        invite.status = "declined"
        invite.is_used = True
        invite.responded_at = datetime.now(timezone.utc)
    await db.delete(target)
    await db.commit()
    return True, "kicked"


async def ban_server_member(
    db: AsyncSession,
    server_id: int,
    actor_id: int,
    target_id: int,
    reason: str | None = None,
) -> tuple[bool, str]:
    await ensure_server_moderation_tables(db)
    server = await _get_server_for_moderation(db, server_id)
    actor = await get_server_member(db, server_id, actor_id)
    target = await get_server_member(db, server_id, target_id)
    if not server or not target:
        return False, "member_not_found"
    if not _can_act_on_member(server, actor, target):
        return False, "forbidden"
    existing = await db.execute(
        select(ServerBan).where(
            ServerBan.server_id == int(server_id),
            ServerBan.account_id == int(target_id),
        )
    )
    if not existing.scalar_one_or_none():
        db.add(ServerBan(
            server_id=int(server_id),
            account_id=int(target_id),
            banned_by_id=int(actor_id),
            reason=(reason or "").strip()[:255] or None,
        ))
    pending = await db.execute(
        select(ServerInvite).where(
            ServerInvite.server_id == int(server_id),
            ServerInvite.invitee_id == int(target_id),
            ServerInvite.status == "pending",
        )
    )
    for invite in pending.scalars().all():
        invite.status = "declined"
        invite.is_used = True
        invite.responded_at = datetime.now(timezone.utc)
    await db.delete(target)
    await db.commit()
    return True, "banned"


async def unban_server_member(
    db: AsyncSession,
    server_id: int,
    actor_id: int,
    target_id: int,
) -> tuple[bool, str]:
    await ensure_server_moderation_tables(db)
    if not await can_manage_server(db, server_id, actor_id):
        return False, "forbidden"
    result = await db.execute(
        select(ServerBan).where(
            ServerBan.server_id == int(server_id),
            ServerBan.account_id == int(target_id),
        )
    )
    ban = result.scalar_one_or_none()
    if not ban:
        return False, "ban_not_found"
    await db.delete(ban)
    await db.commit()
    return True, "unbanned"


async def transfer_server_ownership(
    db: AsyncSession,
    server_id: int,
    actor_id: int,
    target_id: int,
) -> tuple[bool, str]:
    # All moderation mutations lock the same server row, so a role change and
    # an ownership transfer cannot race and leave inconsistent owner roles.
    server = await _get_server_for_moderation(db, server_id)
    actor = await get_server_member(db, server_id, actor_id)
    target = await get_server_member(db, server_id, target_id)
    if not server or not actor or not target:
        return False, "member_not_found"
    if int(server.owner_id) != int(actor_id):
        return False, "owner_only"
    if int(actor_id) == int(target_id):
        return False, "already_owner"
    server.owner_id = int(target_id)
    actor.role = "admin"
    target.role = "owner"
    await db.commit()
    await db.refresh(server)
    return True, "transferred"


async def invite_friend_to_server(
    db: AsyncSession,
    server_id: int,
    inviter_id: int,
    invitee_id: int,
) -> ServerInvite | None:
    await ensure_server_invite_columns(db)
    if inviter_id == invitee_id:
        return None
    if not await is_server_member(db, server_id, inviter_id):
        return None
    if await is_server_member(db, server_id, invitee_id):
        return None
    if await is_server_banned(db, server_id, invitee_id):
        return None

    # Only friends can be invited, keeps random spam out.
    if await friendship_status(db, inviter_id, invitee_id) != "friends":
        return None

    result = await db.execute(
        select(ServerInvite).where(ServerInvite.server_id == server_id, ServerInvite.invitee_id == invitee_id)
    )
    existing = result.scalar_one_or_none()
    if existing and existing.status == "pending" and not bool(getattr(existing, "is_used", False)):
        # Legacy pending rows may not have a code yet. Give them a real random code.
        if not getattr(existing, "code", None):
            existing.code = await _generate_unique_invite_code(db)
            await db.commit()
            await db.refresh(existing)
        return existing

    code = await _generate_unique_invite_code(db)
    if existing:
        existing.inviter_id = inviter_id
        existing.status = "pending"
        existing.is_used = False
        existing.responded_at = None
        existing.code = code
        await db.commit()
        await db.refresh(existing)
        return existing

    invite = ServerInvite(
        server_id=server_id,
        inviter_id=inviter_id,
        invitee_id=invitee_id,
        status="pending",
        is_used=False,
        code=code,
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return invite


async def list_pending_server_invites(db: AsyncSession, account_id: int) -> list[dict]:
    await ensure_server_invite_columns(db)
    result = await db.execute(
        select(ServerInvite)
        .where(ServerInvite.invitee_id == account_id, ServerInvite.status == "pending")
        .order_by(ServerInvite.created_at.desc())
    )
    items = []
    for invite in result.scalars().all():
        server = await get_server_by_id(db, invite.server_id)
        inviter = await get_account_by_id(db, invite.inviter_id)
        items.append({"invite": invite, "server": server, "inviter": inviter})
    return items


async def respond_server_invite(
    db: AsyncSession,
    invite_code: str | int,
    account_id: int,
    accept: bool,
) -> ServerInvite | None:
    await ensure_server_invite_columns(db)
    invite = await get_server_invite_by_ref(db, str(invite_code))
    if not invite or int(invite.invitee_id) != int(account_id):
        return None
    if invite.status != "pending" or bool(getattr(invite, "is_used", False)):
        return None

    invite.status = "accepted" if accept else "declined"
    invite.is_used = True
    invite.responded_at = datetime.now(timezone.utc)

    if accept and not await is_server_member(db, invite.server_id, account_id):
        db.add(ServerMember(server_id=invite.server_id, account_id=account_id, role="member"))

    await db.commit()
    await db.refresh(invite)
    return invite


async def get_server_invite_by_ref(db: AsyncSession, invite_ref: str | int) -> ServerInvite | None:
    await ensure_server_invite_columns(db)
    raw = str(invite_ref or "").strip()
    if not raw:
        return None

    # New secure path: random invite code.
    result = await db.execute(select(ServerInvite).where(ServerInvite.code == raw))
    invite = result.scalar_one_or_none()
    if invite:
        return invite

    # Legacy fallback only for old DM messages that still contain an old invite row id.
    # This does NOT allow joining arbitrary servers by server_id.
    if raw.isdigit():
        result = await db.execute(select(ServerInvite).where(ServerInvite.id == int(raw)))
        return result.scalar_one_or_none()
    return None


async def get_server_invite_by_id(db: AsyncSession, invite_id: int) -> ServerInvite | None:
    return await get_server_invite_by_ref(db, invite_id)


async def build_server_invite_preview(
    db: AsyncSession,
    invite_code: str | int,
    viewer_id: int,
    channel_id: int | None = None,
) -> dict:
    """Return a plain JSON-safe invite preview.

    This intentionally returns a dict instead of ORM objects so the template never
    touches lazy/expired SQLAlchemy state. One broken invite must not break DM view.
    """
    await ensure_server_invite_columns(db)
    try:
        invite = await get_server_invite_by_ref(db, invite_code)
        if not invite:
            return {"type": "server_invite", "invite_id": str(invite_code), "invite_code": str(invite_code), "valid": False, "status": "invalid"}

        server = await get_server_by_id(db, invite.server_id)
        if not server:
            return {"type": "server_invite", "invite_id": str(invite_code), "invite_code": str(invite_code), "valid": False, "status": "invalid"}

        channel = None
        if channel_id:
            channel = await get_server_channel(db, invite.server_id, channel_id)
            if not channel:
                channel_id = None

        inviter = await get_account_by_id(db, invite.inviter_id)
        count_result = await db.execute(
            select(func.count()).select_from(ServerMember).where(ServerMember.server_id == invite.server_id)
        )
        members_count = int(count_result.scalar_one() or 0)
        viewer_is_invitee = int(viewer_id) == int(invite.invitee_id)
        viewer_is_inviter = int(viewer_id) == int(invite.inviter_id)
        already_member = await is_server_member(db, invite.server_id, viewer_id)
        valid = invite.status == "pending" and not bool(getattr(invite, "is_used", False)) and (viewer_is_invitee or viewer_is_inviter)
        public_ref = str(getattr(invite, "code", None) or invite.id)

        return {
            "type": "server_invite",
            "invite_id": public_ref,
            "invite_code": public_ref,
            "legacy_invite_id": int(invite.id),
            "server_id": int(server.id),
            "server_name": server.name,
            "server_icon_url": server.icon_url or "",
            "server_description": server.description or "",
            "channel_id": int(channel.id) if channel else None,
            "channel_name": channel.name if channel else "",
            "inviter_id": int(invite.inviter_id),
            "inviter_username": inviter.username if inviter else "user",
            "invitee_id": int(invite.invitee_id),
            "viewer_is_invitee": viewer_is_invitee,
            "viewer_is_inviter": viewer_is_inviter,
            "already_member": bool(already_member),
            "status": invite.status,
            "is_used": bool(getattr(invite, "is_used", False)),
            "valid": bool(valid),
            "members_count": members_count,
        }
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        return {"type": "server_invite", "invite_id": str(invite_code), "invite_code": str(invite_code), "valid": False, "status": "error"}







# Render/free-tier safe schema guard for message metadata.
# Startup usually runs this, but DM WebSocket/form handlers call it too,
# because create_all() never adds columns to old PostgreSQL tables.
_MESSAGE_META_COLUMNS_READY = False

async def ensure_message_meta_columns(db: AsyncSession) -> None:
    global _MESSAGE_META_COLUMNS_READY
    if _MESSAGE_META_COLUMNS_READY:
        return
    await db.execute(text("ALTER TABLE community_server_messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP WITH TIME ZONE"))
    await db.execute(text("ALTER TABLE community_direct_messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP WITH TIME ZONE"))
    await db.execute(text("ALTER TABLE community_server_messages ADD COLUMN IF NOT EXISTS reply_to_id INTEGER"))
    await db.execute(text("ALTER TABLE community_direct_messages ADD COLUMN IF NOT EXISTS reply_to_id INTEGER"))
    await db.execute(text("ALTER TABLE community_server_messages ADD COLUMN IF NOT EXISTS is_forwarded BOOLEAN NOT NULL DEFAULT FALSE"))
    await db.execute(text("ALTER TABLE community_direct_messages ADD COLUMN IF NOT EXISTS is_forwarded BOOLEAN NOT NULL DEFAULT FALSE"))
    await db.commit()
    _MESSAGE_META_COLUMNS_READY = True


# ============================================================================
# Direct messages
# ============================================================================

def _normalize_dm_pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


async def get_dm_thread_between(db: AsyncSession, account_a_id: int, account_b_id: int) -> DirectThread | None:
    low_id, high_id = _normalize_dm_pair(account_a_id, account_b_id)
    result = await db.execute(
        select(DirectThread).where(
            DirectThread.user_low_id == low_id,
            DirectThread.user_high_id == high_id,
        )
    )
    return result.scalar_one_or_none()


async def get_dm_thread_by_id(db: AsyncSession, thread_id: int) -> DirectThread | None:
    result = await db.execute(select(DirectThread).where(DirectThread.id == thread_id))
    return result.scalar_one_or_none()


async def is_dm_participant(db: AsyncSession, thread_id: int, account_id: int) -> bool:
    thread = await get_dm_thread_by_id(db, thread_id)
    return bool(thread and account_id in {thread.user_low_id, thread.user_high_id})


async def get_or_create_dm_thread(db: AsyncSession, account_a_id: int, account_b_id: int) -> DirectThread | None:
    if account_a_id == account_b_id:
        return None

    account_a = await get_account_by_id(db, account_a_id)
    account_b = await get_account_by_id(db, account_b_id)
    if not account_a or not account_b:
        return None

    low_id, high_id = _normalize_dm_pair(account_a_id, account_b_id)
    existing = await get_dm_thread_between(db, low_id, high_id)
    if existing:
        return existing

    thread = DirectThread(user_low_id=low_id, user_high_id=high_id)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return thread


async def create_dm_message(
    db: AsyncSession,
    thread_id: int,
    author_id: int,
    content: str,
    image_url: str | None = None,
    reply_to_id: int | None = None,
    is_forwarded: bool = False,
) -> DirectMessage:
    await ensure_message_meta_columns(db)
    message = DirectMessage(
        thread_id=thread_id,
        author_id=author_id,
        reply_to_id=reply_to_id,
        is_forwarded=bool(is_forwarded),
        content=content.strip(),
        image_url=image_url.strip() if image_url and image_url.strip() else None,
    )
    db.add(message)

    thread = await get_dm_thread_by_id(db, thread_id)
    if thread:
        thread.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(message)
    return message


async def list_dm_messages(db: AsyncSession, thread_id: int, limit: int = 80) -> list[dict]:
    await ensure_message_meta_columns(db)
    result = await db.execute(
        select(DirectMessage)
        .where(DirectMessage.thread_id == thread_id)
        .order_by(DirectMessage.created_at.desc())
        .limit(limit)
    )
    messages = list(reversed(list(result.scalars().all())))
    feed: list[dict] = []
    for message in messages:
        author = await get_account_by_id(db, message.author_id)
        reply = None
        reply_id = getattr(message, "reply_to_id", None)
        if reply_id:
            reply_msg = await get_dm_message(db, thread_id, int(reply_id))
            if reply_msg:
                reply_author = await get_account_by_id(db, reply_msg.author_id)
                reply = {"message": reply_msg, "author": reply_author}
        feed.append({"message": message, "author": author, "reply": reply})
    return feed


async def list_dm_messages_after(
    db: AsyncSession,
    thread_id: int,
    after_id: int,
    limit: int = 201,
) -> list[DirectMessage]:
    """Return DM messages missed while the thread WebSocket was reconnecting."""
    await ensure_message_meta_columns(db)
    safe_limit = max(1, min(int(limit or 201), 501))
    result = await db.execute(
        select(DirectMessage)
        .where(
            DirectMessage.thread_id == int(thread_id),
            DirectMessage.id > max(0, int(after_id or 0)),
        )
        .order_by(DirectMessage.id.asc())
        .limit(safe_limit)
    )
    return list(result.scalars().all())


async def get_dm_message(db: AsyncSession, thread_id: int, message_id: int) -> DirectMessage | None:
    await ensure_message_meta_columns(db)
    result = await db.execute(
        select(DirectMessage).where(
            DirectMessage.id == message_id,
            DirectMessage.thread_id == thread_id,
        )
    )
    return result.scalar_one_or_none()


async def get_dm_message_by_id(db: AsyncSession, message_id: int) -> DirectMessage | None:
    """Return a DM message by id without knowing the thread first.

    Used by forward-message flow. Authorization is checked in the router with
    is_dm_participant(), so this helper intentionally only fetches.
    """
    await ensure_message_meta_columns(db)
    result = await db.execute(select(DirectMessage).where(DirectMessage.id == message_id))
    return result.scalar_one_or_none()


async def update_dm_message(
    db: AsyncSession,
    message: DirectMessage,
    content: str,
    image_url: str | None = None,
) -> DirectMessage:
    await ensure_message_meta_columns(db)
    if getattr(message, "is_forwarded", False):
        return message
    message.content = content.strip()
    message.image_url = image_url.strip() if image_url and image_url.strip() else None
    message.edited_at = datetime.now(timezone.utc)

    thread = await get_dm_thread_by_id(db, message.thread_id)
    if thread:
        thread.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(message)
    return message


async def delete_dm_message(db: AsyncSession, message: DirectMessage) -> None:
    thread_id = message.thread_id
    await db.delete(message)
    thread = await get_dm_thread_by_id(db, thread_id)
    if thread:
        thread.updated_at = datetime.now(timezone.utc)
    await db.commit()


async def _last_dm_message(db: AsyncSession, thread_id: int) -> DirectMessage | None:
    result = await db.execute(
        select(DirectMessage)
        .where(DirectMessage.thread_id == thread_id)
        .order_by(DirectMessage.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_dm_threads_for_account(db: AsyncSession, account_id: int, limit: int = 40) -> list[dict]:
    result = await db.execute(
        select(DirectThread)
        .where(or_(DirectThread.user_low_id == account_id, DirectThread.user_high_id == account_id))
        .order_by(DirectThread.updated_at.desc())
        .limit(limit)
    )
    threads = list(result.scalars().all())

    items: list[dict] = []
    for thread in threads:
        other_id = thread.user_high_id if thread.user_low_id == account_id else thread.user_low_id
        other = await get_account_by_id(db, other_id)
        if not other:
            continue
        last_message = await _last_dm_message(db, thread.id)
        items.append({"thread": thread, "other": other, "last_message": last_message})
    return items




async def list_forward_targets(db: AsyncSession, account_id: int) -> dict:
    """Targets shown in the Discord-like forward modal.

    DMs are limited to friends. Channel targets are every text channel in every
    server where the current user is a member.

    getattr() keeps this endpoint compatible during rolling deploys where an
    older worker may still use the pre-display-name model.
    """
    friends = await list_friends(db, account_id)
    servers = await list_servers_for_account(db, account_id)

    dm_targets: list[dict] = []
    for friend in friends:
        username = getattr(friend, "username", "") or ""
        title = (getattr(friend, "display_name", None) or username).strip()
        if not username:
            continue
        dm_targets.append({
            "type": "dm",
            "username": username,
            "display_name": title or username,
            "avatar_url": getattr(friend, "avatar_url", None) or "",
        })

    channel_targets: list[dict] = []
    for server in servers:
        server_id = int(getattr(server, "id", 0) or 0)
        if not server_id:
            continue
        server_name = getattr(server, "name", "") or "Сервер"
        server_icon_url = getattr(server, "icon_url", None) or ""
        channels = await list_server_channels(db, server_id)
        for channel in channels:
            channel_id = int(getattr(channel, "id", 0) or 0)
            if not channel_id:
                continue
            channel_targets.append({
                "type": "channel",
                "server_id": server_id,
                "server_name": server_name,
                "server_icon_url": server_icon_url,
                "icon_url": server_icon_url,
                "avatar_url": server_icon_url,
                "channel_id": channel_id,
                "channel_name": getattr(channel, "name", "") or "channel",
            })

    return {"dms": dm_targets, "channels": channel_targets}



# ============================================================================
# Persistent message mentions: @username + server-only @everyone
# ============================================================================

_MENTION_TABLE_READY = False
_MENTION_TRAILING = ".,!?;:)]}>»”’\"'"


async def ensure_mention_table(db: AsyncSession) -> None:
    """Create the polymorphic mention table without requiring Alembic.

    A row represents one mentioned account in one message. The message itself
    may belong to either a DM thread or a server channel.
    """
    global _MENTION_TABLE_READY
    if _MENTION_TABLE_READY:
        return
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_message_mentions (
            id BIGSERIAL PRIMARY KEY,
            target_account_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            author_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            message_kind VARCHAR(16) NOT NULL,
            message_id INTEGER NOT NULL,
            thread_id INTEGER,
            server_id INTEGER,
            channel_id INTEGER,
            mention_type VARCHAR(16) NOT NULL DEFAULT 'user',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            read_at TIMESTAMP WITH TIME ZONE
        )
    """))
    await db.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_community_message_mentions_target_message
        ON community_message_mentions (target_account_id, message_kind, message_id)
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_community_message_mentions_target_unread
        ON community_message_mentions (target_account_id, read_at)
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_community_message_mentions_dm_scope
        ON community_message_mentions (target_account_id, thread_id, read_at)
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_community_message_mentions_server_scope
        ON community_message_mentions (target_account_id, server_id, channel_id, read_at)
    """))
    await db.commit()
    _MENTION_TABLE_READY = True


def extract_mention_names(content: str | None) -> set[str]:
    """Return case-folded names found after @.

    Registration currently permits a broad set of usernames, so parsing stops
    at whitespace and strips only normal sentence punctuation from the end.
    """
    found: set[str] = set()
    for match in re.finditer(r"(?<![\w@])@([^\s@]{1,80})", content or "", flags=re.UNICODE):
        name = (match.group(1) or "").rstrip(_MENTION_TRAILING).strip()
        if name:
            found.add(name.casefold())
    return found


async def _sync_message_mentions(
    db: AsyncSession,
    *,
    message_kind: str,
    message_id: int,
    author_id: int,
    target_types: dict[int, str],
    thread_id: int | None = None,
    server_id: int | None = None,
    channel_id: int | None = None,
) -> tuple[list[int], list[int]]:
    """Synchronize recipients while preserving read state for unchanged targets.

    Returns:
      mentioned_targets: recipients present in the new message content
      affected_targets: union of old and new recipients, useful for live counts
    """
    await ensure_mention_table(db)
    existing_result = await db.execute(
        text("""
            SELECT target_account_id, mention_type
            FROM community_message_mentions
            WHERE message_kind = :message_kind AND message_id = :message_id
        """),
        {"message_kind": message_kind, "message_id": int(message_id)},
    )
    existing = {int(row[0]): str(row[1] or "user") for row in existing_result.fetchall()}
    new_targets = {int(uid): str(kind or "user") for uid, kind in target_types.items() if int(uid) != int(author_id)}
    affected = set(existing) | set(new_targets)

    removed = set(existing) - set(new_targets)
    for target_id in removed:
        await db.execute(
            text("""
                DELETE FROM community_message_mentions
                WHERE message_kind = :message_kind
                  AND message_id = :message_id
                  AND target_account_id = :target_account_id
            """),
            {
                "message_kind": message_kind,
                "message_id": int(message_id),
                "target_account_id": int(target_id),
            },
        )

    for target_id, mention_type in new_targets.items():
        await db.execute(
            text("""
                INSERT INTO community_message_mentions (
                    target_account_id, author_id, message_kind, message_id,
                    thread_id, server_id, channel_id, mention_type
                )
                VALUES (
                    :target_account_id, :author_id, :message_kind, :message_id,
                    :thread_id, :server_id, :channel_id, :mention_type
                )
                ON CONFLICT (target_account_id, message_kind, message_id)
                DO UPDATE SET
                    author_id = EXCLUDED.author_id,
                    thread_id = EXCLUDED.thread_id,
                    server_id = EXCLUDED.server_id,
                    channel_id = EXCLUDED.channel_id,
                    mention_type = EXCLUDED.mention_type
            """),
            {
                "target_account_id": target_id,
                "author_id": int(author_id),
                "message_kind": message_kind,
                "message_id": int(message_id),
                "thread_id": int(thread_id) if thread_id else None,
                "server_id": int(server_id) if server_id else None,
                "channel_id": int(channel_id) if channel_id else None,
                "mention_type": mention_type,
            },
        )

    await db.commit()
    return sorted(new_targets), sorted(affected)


async def sync_dm_mentions(
    db: AsyncSession,
    message: DirectMessage,
) -> tuple[list[int], list[int]]:
    await ensure_mention_table(db)
    thread = await get_dm_thread_by_id(db, int(message.thread_id))
    if not thread:
        return [], []

    tokens = extract_mention_names(message.content)
    tokens.discard("everyone")  # @everyone is intentionally server-only.

    target_types: dict[int, str] = {}
    for account_id in (int(thread.user_low_id), int(thread.user_high_id)):
        if account_id == int(message.author_id):
            continue
        account = await get_account_by_id(db, account_id)
        if account and (account.username or "").casefold() in tokens:
            target_types[account_id] = "user"

    return await _sync_message_mentions(
        db,
        message_kind="dm",
        message_id=int(message.id),
        author_id=int(message.author_id),
        thread_id=int(message.thread_id),
        target_types=target_types,
    )


async def sync_server_mentions(
    db: AsyncSession,
    message: ServerMessage,
) -> tuple[list[int], list[int]]:
    await ensure_mention_table(db)
    tokens = extract_mention_names(message.content)
    members = await list_server_members(db, int(message.server_id))

    target_types: dict[int, str] = {}
    if "everyone" in tokens:
        for item in members:
            account = item.get("account")
            if account and int(account.id) != int(message.author_id):
                target_types[int(account.id)] = "everyone"

    for item in members:
        account = item.get("account")
        if not account or int(account.id) == int(message.author_id):
            continue
        if (account.username or "").casefold() in tokens:
            target_types[int(account.id)] = "user"

    return await _sync_message_mentions(
        db,
        message_kind="server",
        message_id=int(message.id),
        author_id=int(message.author_id),
        server_id=int(message.server_id),
        channel_id=int(message.channel_id),
        target_types=target_types,
    )


async def delete_message_mentions(
    db: AsyncSession,
    message_kind: str,
    message_id: int,
) -> list[int]:
    await ensure_mention_table(db)
    result = await db.execute(
        text("""
            SELECT target_account_id
            FROM community_message_mentions
            WHERE message_kind = :message_kind AND message_id = :message_id
        """),
        {"message_kind": message_kind, "message_id": int(message_id)},
    )
    affected = sorted({int(row[0]) for row in result.fetchall()})
    await db.execute(
        text("""
            DELETE FROM community_message_mentions
            WHERE message_kind = :message_kind AND message_id = :message_id
        """),
        {"message_kind": message_kind, "message_id": int(message_id)},
    )
    await db.commit()
    return affected


async def mark_dm_mentions_read(db: AsyncSession, account_id: int, thread_id: int) -> bool:
    await ensure_mention_table(db)
    result = await db.execute(
        text("""
            UPDATE community_message_mentions
            SET read_at = NOW()
            WHERE target_account_id = :account_id
              AND message_kind = 'dm'
              AND thread_id = :thread_id
              AND read_at IS NULL
        """),
        {"account_id": int(account_id), "thread_id": int(thread_id)},
    )
    await db.commit()
    return bool(getattr(result, "rowcount", 0))


async def mark_server_channel_mentions_read(
    db: AsyncSession,
    account_id: int,
    server_id: int,
    channel_id: int,
) -> bool:
    await ensure_mention_table(db)
    result = await db.execute(
        text("""
            UPDATE community_message_mentions
            SET read_at = NOW()
            WHERE target_account_id = :account_id
              AND message_kind = 'server'
              AND server_id = :server_id
              AND channel_id = :channel_id
              AND read_at IS NULL
        """),
        {
            "account_id": int(account_id),
            "server_id": int(server_id),
            "channel_id": int(channel_id),
        },
    )
    await db.commit()
    return bool(getattr(result, "rowcount", 0))


async def unread_mention_summary(db: AsyncSession, account_id: int) -> dict:
    await ensure_mention_table(db)

    dm_result = await db.execute(
        text("""
            SELECT thread_id, COUNT(*)
            FROM community_message_mentions
            WHERE target_account_id = :account_id
              AND message_kind = 'dm'
              AND read_at IS NULL
              AND thread_id IS NOT NULL
            GROUP BY thread_id
        """),
        {"account_id": int(account_id)},
    )
    dm_by_thread = {int(row[0]): int(row[1]) for row in dm_result.fetchall()}

    server_result = await db.execute(
        text("""
            SELECT server_id, COUNT(*)
            FROM community_message_mentions
            WHERE target_account_id = :account_id
              AND message_kind = 'server'
              AND read_at IS NULL
              AND server_id IS NOT NULL
            GROUP BY server_id
        """),
        {"account_id": int(account_id)},
    )
    server_by_id = {int(row[0]): int(row[1]) for row in server_result.fetchall()}

    channel_result = await db.execute(
        text("""
            SELECT server_id, channel_id, COUNT(*)
            FROM community_message_mentions
            WHERE target_account_id = :account_id
              AND message_kind = 'server'
              AND read_at IS NULL
              AND server_id IS NOT NULL
              AND channel_id IS NOT NULL
            GROUP BY server_id, channel_id
        """),
        {"account_id": int(account_id)},
    )
    channel_by_id = {
        f"{int(row[0])}:{int(row[1])}": int(row[2])
        for row in channel_result.fetchall()
    }

    dm_total = sum(dm_by_thread.values())
    server_total = sum(server_by_id.values())
    return {
        "total": dm_total + server_total,
        "dm_total": dm_total,
        "server_total": server_total,
        "dm_by_thread": dm_by_thread,
        "server_by_id": server_by_id,
        "channel_by_id": channel_by_id,
    }


# ============================================================================
# Gifts
# ============================================================================

async def list_gifts(db: AsyncSession) -> list[Gift]:
    result = await db.execute(select(Gift).order_by(Gift.created_at.desc()))
    return list(result.scalars().all())


async def get_gift_by_id(db: AsyncSession, gift_id: int) -> Gift | None:
    result = await db.execute(select(Gift).where(Gift.id == gift_id))
    return result.scalar_one_or_none()


async def create_gift(
    db: AsyncSession,
    name: str,
    image_url: str,
    description: str | None = None,
) -> Gift:
    gift = Gift(
        name=name.strip(),
        image_url=image_url.strip(),
        description=description.strip() if description else None,
    )
    db.add(gift)
    await db.commit()
    await db.refresh(gift)
    return gift


async def delete_gift(db: AsyncSession, gift_id: int) -> bool:
    gift = await get_gift_by_id(db, gift_id)
    if not gift:
        return False

    # First remove issued copies of this gift so PostgreSQL foreign keys
    # do not block deleting the catalog item.
    instances_result = await db.execute(
        select(GiftInstance).where(GiftInstance.gift_id == gift_id)
    )
    for instance in instances_result.scalars().all():
        await db.delete(instance)

    await db.delete(gift)
    await db.commit()
    return True


async def give_gift_to_account(
    db: AsyncSession,
    recipient_id: int,
    gift_id: int,
    gifted_by: str | None = "Адміністрація",
    message: str | None = None,
) -> GiftInstance | None:
    recipient = await get_account_by_id(db, recipient_id)
    gift = await get_gift_by_id(db, gift_id)

    if not recipient or not gift:
        return None

    gift_instance = GiftInstance(
        gift_id=gift_id,
        recipient_id=recipient_id,
        gifted_by=gifted_by.strip() if gifted_by else "Адміністрація",
        message=message.strip() if message else None,
    )
    db.add(gift_instance)
    await db.commit()
    await db.refresh(gift_instance)
    return gift_instance


async def list_gifts_for_account(db: AsyncSession, account_id: int) -> list[GiftInstance]:
    result = await db.execute(
        select(GiftInstance)
        .options(selectinload(GiftInstance.gift))
        .where(GiftInstance.recipient_id == account_id)
        .order_by(GiftInstance.created_at.desc())
    )
    return list(result.scalars().all())


# ============================================================================
# Pinned messages: DM + server channel pins.
# ============================================================================

_PIN_TABLES_READY = False


async def ensure_pin_tables(db: AsyncSession) -> None:
    global _PIN_TABLES_READY
    if _PIN_TABLES_READY:
        return
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_direct_message_pins (
            id SERIAL PRIMARY KEY,
            thread_id INTEGER NOT NULL REFERENCES community_direct_threads(id) ON DELETE CASCADE,
            message_id INTEGER NOT NULL REFERENCES community_direct_messages(id) ON DELETE CASCADE,
            pinned_by_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_community_direct_message_pins_message_id ON community_direct_message_pins (message_id)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_community_direct_message_pins_thread_created ON community_direct_message_pins (thread_id, created_at DESC)"))

    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_server_message_pins (
            id SERIAL PRIMARY KEY,
            server_id INTEGER NOT NULL REFERENCES community_servers(id) ON DELETE CASCADE,
            channel_id INTEGER NOT NULL REFERENCES community_server_channels(id) ON DELETE CASCADE,
            message_id INTEGER NOT NULL REFERENCES community_server_messages(id) ON DELETE CASCADE,
            pinned_by_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_community_server_message_pins_message_id ON community_server_message_pins (message_id)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_community_server_message_pins_channel_created ON community_server_message_pins (server_id, channel_id, created_at DESC)"))
    await db.commit()
    _PIN_TABLES_READY = True


async def _build_dm_pin_payload(db: AsyncSession, *, message: DirectMessage, pinned_by_id: int | None, created_at) -> dict:
    author = await get_account_by_id(db, message.author_id)
    pinned_by = await get_account_by_id(db, pinned_by_id) if pinned_by_id else None
    return {
        "message_id": int(message.id),
        "thread_id": int(message.thread_id),
        "content": message.content or "",
        "image_url": message.image_url or "",
        "created_at": message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
        "pinned_at": created_at.isoformat() if created_at else None,
        "author": {
            "id": int(author.id) if author else None,
            "username": author.username if author else "видалений юзер",
            "avatar_url": author.avatar_url if author else "",
        },
        "pinned_by": {
            "id": int(pinned_by.id) if pinned_by else None,
            "username": pinned_by.username if pinned_by else "",
        },
    }


async def list_dm_pins(db: AsyncSession, thread_id: int) -> list[dict]:
    await ensure_pin_tables(db)
    rows = (await db.execute(text("""
        SELECT message_id, pinned_by_id, created_at
        FROM community_direct_message_pins
        WHERE thread_id = :thread_id
        ORDER BY created_at DESC
    """), {"thread_id": int(thread_id)})).mappings().all()
    result: list[dict] = []
    for row in rows:
        message = await get_dm_message(db, thread_id, int(row["message_id"]))
        if not message:
            continue
        result.append(await _build_dm_pin_payload(db, message=message, pinned_by_id=row["pinned_by_id"], created_at=row["created_at"]))
    return result


async def pin_dm_message(db: AsyncSession, thread_id: int, message_id: int, pinned_by_id: int) -> tuple[dict | None, bool]:
    await ensure_pin_tables(db)
    message = await get_dm_message(db, thread_id, message_id)
    if not message:
        return None, False
    existing = (await db.execute(text("""
        SELECT pinned_by_id, created_at
        FROM community_direct_message_pins
        WHERE message_id = :message_id
        LIMIT 1
    """), {"message_id": int(message_id)})).mappings().first()
    if existing:
        return await _build_dm_pin_payload(db, message=message, pinned_by_id=existing["pinned_by_id"], created_at=existing["created_at"]), False
    await db.execute(text("""
        INSERT INTO community_direct_message_pins (thread_id, message_id, pinned_by_id)
        VALUES (:thread_id, :message_id, :pinned_by_id)
    """), {"thread_id": int(thread_id), "message_id": int(message_id), "pinned_by_id": int(pinned_by_id)})
    await db.commit()
    created_row = (await db.execute(text("""
        SELECT pinned_by_id, created_at
        FROM community_direct_message_pins
        WHERE message_id = :message_id
        LIMIT 1
    """), {"message_id": int(message_id)})).mappings().first()
    return await _build_dm_pin_payload(db, message=message, pinned_by_id=created_row["pinned_by_id"], created_at=created_row["created_at"]), True


async def unpin_dm_message(db: AsyncSession, thread_id: int, message_id: int) -> bool:
    await ensure_pin_tables(db)
    row = (await db.execute(text("""
        DELETE FROM community_direct_message_pins
        WHERE thread_id = :thread_id AND message_id = :message_id
        RETURNING id
    """), {"thread_id": int(thread_id), "message_id": int(message_id)})).first()
    await db.commit()
    return bool(row)


async def _build_server_pin_payload(db: AsyncSession, *, message: ServerMessage, pinned_by_id: int | None, created_at) -> dict:
    author = await get_account_by_id(db, message.author_id)
    pinned_by = await get_account_by_id(db, pinned_by_id) if pinned_by_id else None
    return {
        "message_id": int(message.id),
        "server_id": int(message.server_id),
        "channel_id": int(message.channel_id),
        "content": message.content or "",
        "image_url": message.image_url or "",
        "created_at": message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
        "pinned_at": created_at.isoformat() if created_at else None,
        "author": {
            "id": int(author.id) if author else None,
            "username": author.username if author else "видалений юзер",
            "avatar_url": author.avatar_url if author else "",
        },
        "pinned_by": {
            "id": int(pinned_by.id) if pinned_by else None,
            "username": pinned_by.username if pinned_by else "",
        },
    }


async def list_server_pins(db: AsyncSession, server_id: int, channel_id: int) -> list[dict]:
    await ensure_pin_tables(db)
    rows = (await db.execute(text("""
        SELECT message_id, pinned_by_id, created_at
        FROM community_server_message_pins
        WHERE server_id = :server_id AND channel_id = :channel_id
        ORDER BY created_at DESC
    """), {"server_id": int(server_id), "channel_id": int(channel_id)})).mappings().all()
    result: list[dict] = []
    for row in rows:
        message = await get_server_message(db, server_id, channel_id, int(row["message_id"]))
        if not message:
            continue
        result.append(await _build_server_pin_payload(db, message=message, pinned_by_id=row["pinned_by_id"], created_at=row["created_at"]))
    return result


async def pin_server_message(db: AsyncSession, server_id: int, channel_id: int, message_id: int, pinned_by_id: int) -> tuple[dict | None, bool]:
    await ensure_pin_tables(db)
    message = await get_server_message(db, server_id, channel_id, message_id)
    if not message:
        return None, False
    existing = (await db.execute(text("""
        SELECT pinned_by_id, created_at
        FROM community_server_message_pins
        WHERE message_id = :message_id
        LIMIT 1
    """), {"message_id": int(message_id)})).mappings().first()
    if existing:
        return await _build_server_pin_payload(db, message=message, pinned_by_id=existing["pinned_by_id"], created_at=existing["created_at"]), False
    await db.execute(text("""
        INSERT INTO community_server_message_pins (server_id, channel_id, message_id, pinned_by_id)
        VALUES (:server_id, :channel_id, :message_id, :pinned_by_id)
    """), {"server_id": int(server_id), "channel_id": int(channel_id), "message_id": int(message_id), "pinned_by_id": int(pinned_by_id)})
    await db.commit()
    created_row = (await db.execute(text("""
        SELECT pinned_by_id, created_at
        FROM community_server_message_pins
        WHERE message_id = :message_id
        LIMIT 1
    """), {"message_id": int(message_id)})).mappings().first()
    return await _build_server_pin_payload(db, message=message, pinned_by_id=created_row["pinned_by_id"], created_at=created_row["created_at"]), True


async def unpin_server_message(db: AsyncSession, server_id: int, channel_id: int, message_id: int) -> bool:
    await ensure_pin_tables(db)
    row = (await db.execute(text("""
        DELETE FROM community_server_message_pins
        WHERE server_id = :server_id AND channel_id = :channel_id AND message_id = :message_id
        RETURNING id
    """), {"server_id": int(server_id), "channel_id": int(channel_id), "message_id": int(message_id)})).first()
    await db.commit()
    return bool(row)


# ============================================================================
# Nitro-like one-time gift codes + user subscription credits.
# ============================================================================

_NITRO_TABLES_READY = False
NITRO_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
NITRO_TIER_LABELS = {
    "basic": "AlexiHub Nitro",
    "gold": "Золото Nitro",
    "platinum": "Платина Nitro",
    "diamond": "Алмаз Nitro",
    "emerald": "Изумруд Nitro",
    "ruby": "Рубин Nitro",
}

NITRO_TIER_BOOSTS = {
    "basic": 2,
    "gold": 4,
    "platinum": 6,
    "diamond": 8,
    "emerald": 10,
    "ruby": 12,
}

SERVER_BOOST_LEVELS = (
    {"level": 1, "required": 2},
    {"level": 2, "required": 5},
    {"level": 3, "required": 7},
)
SERVER_TAG_REQUIRED_BOOSTS = 3
SERVER_TAG_ICONS = {
    "gem": "◆",
    "crown": "♛",
    "star": "★",
    "shield": "⬡",
    "flame": "♨",
    "leaf": "❧",
    "bolt": "ϟ",
    "heart": "♥",
    "moon": "☾",
    "skull": "☠",
}

NITRO_GIFTER_BADGE_LABELS = {
    "legend": "Легенда",
    "philanthropist": "Филантроп",
    "icon": "Икона",
}


def nitro_gifter_badge_from_count(claimed_gifts: int | float | None) -> dict:
    """Profile gift badge earned from successfully claimed DM Nitro gifts.

    One claimed gift unlocks Legend, two or three unlock Philanthropist,
    and four or more unlock Icon. Pending/unclaimed cards never count.
    """
    count = max(0, int(claimed_gifts or 0))
    if count >= 4:
        tier = "icon"
    elif count >= 2:
        tier = "philanthropist"
    elif count >= 1:
        tier = "legend"
    else:
        return {
            "active": False,
            "count": 0,
            "tier": None,
            "label": None,
            "title": None,
        }
    label = NITRO_GIFTER_BADGE_LABELS[tier]
    return {
        "active": True,
        "count": count,
        "tier": tier,
        "label": label,
        "title": f"Подарки, ур. «{label}»",
    }


def nitro_tier_from_duration(duration_days: int | float | None) -> tuple[str, str]:
    """Return one canonical Nitro tier for the full uninterrupted credit period."""
    days = max(0, int(duration_days or 0))
    if days >= 500:
        tier = "ruby"
    elif days >= 200:
        tier = "emerald"
    elif days >= 101:
        tier = "diamond"
    elif days >= 61:
        tier = "platinum"
    elif days >= 31:
        tier = "gold"
    else:
        tier = "basic"
    return tier, NITRO_TIER_LABELS[tier]


def _nitro_datetime_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def ensure_nitro_tables(db: AsyncSession) -> None:
    global _NITRO_TABLES_READY
    if _NITRO_TABLES_READY:
        return
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_nitro_gift_codes (
            id SERIAL PRIMARY KEY,
            code VARCHAR(64) NOT NULL UNIQUE,
            days INTEGER NOT NULL DEFAULT 30,
            note VARCHAR(255),
            created_by_id INTEGER REFERENCES community_accounts(id) ON DELETE SET NULL,
            used_by_id INTEGER REFERENCES community_accounts(id) ON DELETE SET NULL,
            is_used BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            used_at TIMESTAMP WITH TIME ZONE
        )
    """))
    await db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_community_nitro_gift_codes_code ON community_nitro_gift_codes (code)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_community_nitro_gift_codes_used ON community_nitro_gift_codes (is_used, used_by_id)"))

    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_nitro_subscriptions (
            id SERIAL PRIMARY KEY,
            account_id INTEGER NOT NULL UNIQUE REFERENCES community_accounts(id) ON DELETE CASCADE,
            started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            source_code VARCHAR(64),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_community_nitro_subscriptions_account ON community_nitro_subscriptions (account_id)"))

    # Recipient-bound Nitro gifts sent as special cards inside direct messages.
    # The public token identifies the card but cannot be redeemed by anyone
    # except the recipient stored in this row.
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_nitro_dm_gifts (
            id SERIAL PRIMARY KEY,
            public_token VARCHAR(96) NOT NULL UNIQUE,
            thread_id INTEGER NOT NULL REFERENCES community_direct_threads(id) ON DELETE CASCADE,
            message_id INTEGER UNIQUE REFERENCES community_direct_messages(id) ON DELETE CASCADE,
            sender_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            recipient_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            days INTEGER NOT NULL DEFAULT 30,
            note VARCHAR(255),
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            claimed_at TIMESTAMP WITH TIME ZONE
        )
    """))
    await db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_community_nitro_dm_gifts_token ON community_nitro_dm_gifts (public_token)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_community_nitro_dm_gifts_recipient_status ON community_nitro_dm_gifts (recipient_id, status)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_community_nitro_dm_gifts_thread ON community_nitro_dm_gifts (thread_id, created_at)"))

    await db.commit()
    _NITRO_TABLES_READY = True


def _chunk_nitro_code(raw: str) -> str:
    clean = ''.join(ch for ch in raw.upper() if ch.isalnum())[:20]
    return '-'.join(clean[i:i+5] for i in range(0, len(clean), 5))


def _new_nitro_code() -> str:
    token = ''.join(secrets.choice(NITRO_CODE_ALPHABET) for _ in range(15))
    return 'ALEXI-' + _chunk_nitro_code(token)


def normalize_nitro_code(value: str | None) -> str:
    clean = (value or '').strip().upper()
    clean = re.sub(r'[^A-Z0-9-]', '', clean)
    # Accept ALEXI-XXXXX-XXXXX-XXXXX and also bare XXXXX-XXXXX-XXXXX.
    if clean and not clean.startswith('ALEXI-') and len(clean.replace('-', '')) >= 12:
        clean = 'ALEXI-' + _chunk_nitro_code(clean)
    return clean


async def _generate_unique_nitro_code(db: AsyncSession) -> str:
    await ensure_nitro_tables(db)
    for _ in range(32):
        code = _new_nitro_code()
        row = (await db.execute(text("SELECT id FROM community_nitro_gift_codes WHERE code = :code LIMIT 1"), {"code": code})).first()
        if not row:
            return code
    raise RuntimeError('Could not generate unique Nitro code')


def is_nitro_code_generator(account: Account | None) -> bool:
    if not account:
        return False
    role = (getattr(account, 'role_label', None) or '').strip().lower()
    username = (getattr(account, 'username', None) or '').strip().lower()
    admin_roles = {'owner', 'admin', 'administrator', 'developer', 'dev', 'code', 'staff', 'модер', 'админ'}
    return bool(getattr(account, 'is_verified', False) or role in admin_roles or username in {'alexi', 'anchousxvii'})


async def create_nitro_gift_code(db: AsyncSession, creator_id: int, days: int = 30, note: str | None = None) -> dict:
    await ensure_nitro_tables(db)
    days = max(1, min(int(days or 30), 365))
    code = await _generate_unique_nitro_code(db)
    await db.execute(text("""
        INSERT INTO community_nitro_gift_codes (code, days, note, created_by_id)
        VALUES (:code, :days, :note, :created_by_id)
    """), {
        "code": code,
        "days": days,
        "note": (note or '').strip()[:255] or None,
        "created_by_id": int(creator_id),
    })
    await db.commit()
    row = (await db.execute(text("""
        SELECT code, days, note, created_at
        FROM community_nitro_gift_codes
        WHERE code = :code
    """), {"code": code})).mappings().first()
    if not row:
        return {"code": code, "days": days, "note": note or '', "created_at": None}
    created_at = row["created_at"]
    return {
        "code": row["code"],
        "days": int(row["days"] or days),
        "note": row["note"] or "",
        "created_at": created_at.isoformat() if created_at else None,
    }


def _nitro_payload_from_row(row) -> dict:
    if not row:
        tier, tier_label = nitro_tier_from_duration(0)
        return {
            "active": False,
            "started_at": None,
            "expires_at": None,
            "days_left": 0,
            "duration_days": 0,
            "tier": tier,
            "tier_label": tier_label,
            "source_code": None,
        }

    now = datetime.now(timezone.utc)
    expires_at = _nitro_datetime_utc(row['expires_at'])
    started_at = _nitro_datetime_utc(row['started_at'])
    active = bool(expires_at and expires_at > now)

    days_left = 0
    if active and expires_at:
        seconds = max(0, (expires_at - now).total_seconds())
        days_left = max(1, int((seconds + 86399) // 86400))

    duration_days = 0
    if started_at and expires_at and expires_at > started_at:
        duration_seconds = max(0, (expires_at - started_at).total_seconds())
        duration_days = max(1, int((duration_seconds + 86399) // 86400))

    tier, tier_label = nitro_tier_from_duration(duration_days)
    return {
        "active": active,
        "started_at": started_at.isoformat() if started_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "days_left": days_left,
        "duration_days": duration_days,
        "tier": tier,
        "tier_label": tier_label,
        "source_code": row.get('source_code') if hasattr(row, 'get') else row['source_code'],
    }


async def get_nitro_subscription(db: AsyncSession, account_id: int) -> dict:
    await ensure_nitro_tables(db)
    row = (await db.execute(text("""
        SELECT started_at, expires_at, source_code
        FROM community_nitro_subscriptions
        WHERE account_id = :account_id
        LIMIT 1
    """), {"account_id": int(account_id)})).mappings().first()
    return _nitro_payload_from_row(row)


async def redeem_nitro_gift_code(db: AsyncSession, account_id: int, code_value: str | None) -> dict:
    await ensure_nitro_tables(db)
    code = normalize_nitro_code(code_value)
    if not code:
        return {"ok": False, "error": "bad_code", "message": "Введи код."}

    row = (await db.execute(text("""
        SELECT id, code, days, is_used, used_by_id
        FROM community_nitro_gift_codes
        WHERE code = :code
        LIMIT 1
        FOR UPDATE
    """), {"code": code})).mappings().first()
    if not row:
        return {"ok": False, "error": "not_found", "message": "Код не найден."}
    if bool(row['is_used']):
        return {"ok": False, "error": "used", "message": "Код уже использован."}

    now = datetime.now(timezone.utc)
    current = (await db.execute(text("""
        SELECT started_at, expires_at, source_code
        FROM community_nitro_subscriptions
        WHERE account_id = :account_id
        LIMIT 1
    """), {"account_id": int(account_id)})).mappings().first()
    days = int(row['days'] or 30)

    current_expires_at = _nitro_datetime_utc(current['expires_at']) if current else None
    current_started_at = _nitro_datetime_utc(current['started_at']) if current else None
    current_is_active = bool(current_expires_at and current_expires_at > now)
    started_at = current_started_at if current_is_active and current_started_at else now
    base_time = current_expires_at if current_is_active else now
    expires_at = base_time + timedelta(days=days)

    if current:
        await db.execute(text("""
            UPDATE community_nitro_subscriptions
            SET started_at = :started_at,
                expires_at = :expires_at,
                source_code = :source_code,
                updated_at = NOW()
            WHERE account_id = :account_id
        """), {
            "started_at": started_at,
            "expires_at": expires_at,
            "source_code": code,
            "account_id": int(account_id),
        })
    else:
        await db.execute(text("""
            INSERT INTO community_nitro_subscriptions (account_id, started_at, expires_at, source_code)
            VALUES (:account_id, :started_at, :expires_at, :source_code)
        """), {"account_id": int(account_id), "started_at": started_at, "expires_at": expires_at, "source_code": code})

    await db.execute(text("""
        UPDATE community_nitro_gift_codes
        SET is_used = TRUE,
            used_by_id = :account_id,
            used_at = NOW()
        WHERE id = :id
    """), {"account_id": int(account_id), "id": int(row['id'])})
    await db.commit()

    sub = await get_nitro_subscription(db, account_id)
    return {"ok": True, "code": code, "days_added": days, "subscription": sub}


async def get_nitro_gifter_badge(db: AsyncSession, account_id: int) -> dict:
    """Count only gifts that the recipient actually activated."""
    await ensure_nitro_tables(db)
    row = (await db.execute(text("""
        SELECT COUNT(*) AS claimed_count
        FROM community_nitro_dm_gifts
        WHERE sender_id = :account_id AND status = 'claimed'
    """), {"account_id": int(account_id)})).mappings().first()
    return nitro_gifter_badge_from_count(row["claimed_count"] if row else 0)


async def nitro_profile_payload(db: AsyncSession, account_id: int) -> dict:
    sub = await get_nitro_subscription(db, account_id)
    gifting = await get_nitro_gifter_badge(db, account_id)
    return {
        "active": bool(sub.get('active')),
        "started_at": sub.get('started_at'),
        "expires_at": sub.get('expires_at'),
        "days_left": sub.get('days_left') or 0,
        "duration_days": sub.get('duration_days') or 0,
        "tier": sub.get('tier') or 'basic',
        "tier_label": sub.get('tier_label') or NITRO_TIER_LABELS['basic'],
        "gifting": gifting,
    }


# ============================================================================
# Nitro-backed server boosts + Discord-style primary server tags.
# ============================================================================

_SERVER_BOOST_TABLES_READY = False


async def ensure_server_boost_tables(db: AsyncSession) -> None:
    """Create boost storage without requiring a destructive ORM migration."""
    global _SERVER_BOOST_TABLES_READY
    if _SERVER_BOOST_TABLES_READY:
        return
    await ensure_nitro_tables(db)
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_server_boost_allocations (
            id SERIAL PRIMARY KEY,
            server_id INTEGER NOT NULL REFERENCES community_servers(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            amount INTEGER NOT NULL DEFAULT 1 CHECK (amount > 0),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            UNIQUE (server_id, account_id)
        )
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_community_server_boosts_server
        ON community_server_boost_allocations (server_id, updated_at DESC)
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_community_server_boosts_account
        ON community_server_boost_allocations (account_id, updated_at ASC)
    """))
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_server_boost_settings (
            server_id INTEGER PRIMARY KEY REFERENCES community_servers(id) ON DELETE CASCADE,
            tag_text VARCHAR(4),
            tag_icon VARCHAR(16) NOT NULL DEFAULT 'gem',
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_account_server_tags (
            account_id INTEGER PRIMARY KEY REFERENCES community_accounts(id) ON DELETE CASCADE,
            server_id INTEGER NOT NULL REFERENCES community_servers(id) ON DELETE CASCADE,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_community_account_server_tags_server
        ON community_account_server_tags (server_id)
    """))
    await db.commit()
    _SERVER_BOOST_TABLES_READY = True


def nitro_boost_capacity(subscription: dict | None) -> int:
    if not subscription or not subscription.get("active"):
        return 0
    return int(NITRO_TIER_BOOSTS.get(str(subscription.get("tier") or "basic"), 2))


def _server_boost_level(total_boosts: int | float | None) -> dict:
    total = max(0, int(total_boosts or 0))
    level = 0
    for item in SERVER_BOOST_LEVELS:
        if total >= int(item["required"]):
            level = int(item["level"])
    next_item = next((item for item in SERVER_BOOST_LEVELS if total < int(item["required"])), None)
    if next_item:
        previous_required = next(
            (int(item["required"]) for item in reversed(SERVER_BOOST_LEVELS) if int(item["level"]) == level),
            0,
        )
        span = max(1, int(next_item["required"]) - previous_required)
        progress = max(0.0, min(1.0, (total - previous_required) / span))
        next_level = int(next_item["level"])
        next_required = int(next_item["required"])
    else:
        progress = 1.0
        next_level = None
        next_required = None
    return {
        "level": level,
        "next_level": next_level,
        "next_required": next_required,
        "progress_percent": round(progress * 100, 2),
        "maxed": next_item is None,
    }


def _server_tag_payload(tag_text: str | None, tag_icon: str | None, *, unlocked: bool) -> dict:
    clean_text = (tag_text or "").strip().upper()[:4]
    clean_icon = (tag_icon or "gem").strip().lower()
    if clean_icon not in SERVER_TAG_ICONS:
        clean_icon = "gem"
    active = bool(unlocked and clean_text)
    return {
        "active": active,
        "unlocked": bool(unlocked),
        "text": clean_text,
        "icon": clean_icon,
        "icon_glyph": SERVER_TAG_ICONS[clean_icon],
        "required_boosts": SERVER_TAG_REQUIRED_BOOSTS,
    }


async def reconcile_account_server_boosts(db: AsyncSession, account_id: int) -> dict:
    """Trim newest allocations when a Nitro subscription expires or loses a tier."""
    await ensure_server_boost_tables(db)
    subscription = await get_nitro_subscription(db, account_id)
    capacity = nitro_boost_capacity(subscription)
    rows = (await db.execute(text("""
        SELECT server_id, amount
        FROM community_server_boost_allocations
        WHERE account_id = :account_id
        ORDER BY updated_at ASC, id ASC
    """), {"account_id": int(account_id)})).mappings().all()
    allocated = sum(max(0, int(row["amount"] or 0)) for row in rows)
    excess = max(0, allocated - capacity)
    if excess:
        for row in reversed(rows):
            if excess <= 0:
                break
            amount = max(0, int(row["amount"] or 0))
            remove = min(amount, excess)
            remaining = amount - remove
            if remaining:
                await db.execute(text("""
                    UPDATE community_server_boost_allocations
                    SET amount = :amount, updated_at = NOW()
                    WHERE account_id = :account_id AND server_id = :server_id
                """), {
                    "amount": remaining,
                    "account_id": int(account_id),
                    "server_id": int(row["server_id"]),
                })
            else:
                await db.execute(text("""
                    DELETE FROM community_server_boost_allocations
                    WHERE account_id = :account_id AND server_id = :server_id
                """), {
                    "account_id": int(account_id),
                    "server_id": int(row["server_id"]),
                })
            excess -= remove
        await db.commit()
        allocated = capacity
    return {
        "subscription": subscription,
        "capacity": capacity,
        "allocated": allocated,
        "remaining": max(0, capacity - allocated),
    }


async def _reconcile_server_boosters(db: AsyncSession, server_id: int) -> None:
    await ensure_server_boost_tables(db)
    account_ids = (await db.execute(text("""
        SELECT account_id
        FROM community_server_boost_allocations
        WHERE server_id = :server_id
    """), {"server_id": int(server_id)})).scalars().all()
    for account_id in account_ids:
        await reconcile_account_server_boosts(db, int(account_id))


async def get_server_boost_status(
    db: AsyncSession,
    server_id: int,
    viewer_id: int | None = None,
) -> dict:
    await ensure_server_boost_tables(db)
    await _reconcile_server_boosters(db, server_id)
    viewer = (
        await reconcile_account_server_boosts(db, int(viewer_id))
        if viewer_id
        else {"subscription": {}, "capacity": 0, "allocated": 0, "remaining": 0}
    )
    total = int((await db.execute(text("""
        SELECT COALESCE(SUM(amount), 0)
        FROM community_server_boost_allocations
        WHERE server_id = :server_id
    """), {"server_id": int(server_id)})).scalar_one() or 0)
    level_info = _server_boost_level(total)
    settings_row = (await db.execute(text("""
        SELECT tag_text, tag_icon
        FROM community_server_boost_settings
        WHERE server_id = :server_id
        LIMIT 1
    """), {"server_id": int(server_id)})).mappings().first()
    tag = _server_tag_payload(
        settings_row["tag_text"] if settings_row else "",
        settings_row["tag_icon"] if settings_row else "gem",
        unlocked=total >= SERVER_TAG_REQUIRED_BOOSTS,
    )
    allocated_here = 0
    if viewer_id:
        allocated_here = int((await db.execute(text("""
            SELECT COALESCE(amount, 0)
            FROM community_server_boost_allocations
            WHERE account_id = :account_id AND server_id = :server_id
            LIMIT 1
        """), {"account_id": int(viewer_id), "server_id": int(server_id)})).scalar_one_or_none() or 0)
    activity_rows = (await db.execute(text("""
        SELECT b.account_id, b.amount, b.updated_at,
               a.username, a.display_name, a.avatar_url
        FROM community_server_boost_allocations b
        JOIN community_accounts a ON a.id = b.account_id
        WHERE b.server_id = :server_id
        ORDER BY b.updated_at DESC
        LIMIT 30
    """), {"server_id": int(server_id)})).mappings().all()
    return {
        "server_id": int(server_id),
        "total_boosts": total,
        **level_info,
        "levels": [dict(item, active=total >= int(item["required"])) for item in SERVER_BOOST_LEVELS],
        "tag": tag,
        "viewer": {
            "capacity": int(viewer["capacity"]),
            "allocated_total": int(viewer["allocated"]),
            "remaining": int(viewer["remaining"]),
            "allocated_here": allocated_here,
            "nitro_active": bool(viewer["subscription"].get("active")),
            "nitro_tier": viewer["subscription"].get("tier") or "basic",
            "nitro_label": viewer["subscription"].get("tier_label") or NITRO_TIER_LABELS["basic"],
        },
        "activity": [
            {
                "account_id": int(row["account_id"]),
                "username": row["username"],
                "display_name": row["display_name"] or row["username"],
                "avatar_url": row["avatar_url"] or "",
                "amount": int(row["amount"] or 0),
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }
            for row in activity_rows
        ],
    }


async def change_server_boost_allocation(
    db: AsyncSession,
    server_id: int,
    account_id: int,
    delta: int,
) -> dict:
    await ensure_server_boost_tables(db)
    if not await is_server_member(db, server_id, account_id):
        return {"ok": False, "error": "not_a_member"}
    delta = 1 if int(delta or 0) > 0 else -1
    # Lock the subscription row so two simultaneous clicks cannot spend the
    # same remaining boost.
    await db.execute(text("""
        SELECT account_id
        FROM community_nitro_subscriptions
        WHERE account_id = :account_id
        FOR UPDATE
    """), {"account_id": int(account_id)})
    viewer = await reconcile_account_server_boosts(db, account_id)
    current = int((await db.execute(text("""
        SELECT COALESCE(amount, 0)
        FROM community_server_boost_allocations
        WHERE account_id = :account_id AND server_id = :server_id
        LIMIT 1
    """), {"account_id": int(account_id), "server_id": int(server_id)})).scalar_one_or_none() or 0)
    if delta > 0:
        if not viewer["subscription"].get("active"):
            await db.rollback()
            return {"ok": False, "error": "nitro_required"}
        if int(viewer["remaining"]) <= 0:
            await db.rollback()
            return {"ok": False, "error": "no_boosts_left"}
        await db.execute(text("""
            INSERT INTO community_server_boost_allocations
                (server_id, account_id, amount, created_at, updated_at)
            VALUES (:server_id, :account_id, 1, NOW(), NOW())
            ON CONFLICT (server_id, account_id)
            DO UPDATE SET amount = community_server_boost_allocations.amount + 1,
                          updated_at = NOW()
        """), {"server_id": int(server_id), "account_id": int(account_id)})
    else:
        if current <= 0:
            await db.rollback()
            return {"ok": False, "error": "nothing_to_remove"}
        if current == 1:
            await db.execute(text("""
                DELETE FROM community_server_boost_allocations
                WHERE account_id = :account_id AND server_id = :server_id
            """), {"account_id": int(account_id), "server_id": int(server_id)})
        else:
            await db.execute(text("""
                UPDATE community_server_boost_allocations
                SET amount = amount - 1, updated_at = NOW()
                WHERE account_id = :account_id AND server_id = :server_id
            """), {"account_id": int(account_id), "server_id": int(server_id)})
    await db.commit()
    return {"ok": True, "status": await get_server_boost_status(db, server_id, account_id)}


async def update_server_tag(
    db: AsyncSession,
    server_id: int,
    tag_text: str | None,
    tag_icon: str | None,
) -> dict:
    status = await get_server_boost_status(db, server_id)
    if not status["tag"]["unlocked"]:
        return {"ok": False, "error": "tag_locked", "required_boosts": SERVER_TAG_REQUIRED_BOOSTS}
    clean_text = re.sub(r"[^A-Z0-9]", "", (tag_text or "").strip().upper())[:4]
    if len(clean_text) < 2:
        return {"ok": False, "error": "bad_tag"}
    clean_icon = (tag_icon or "gem").strip().lower()
    if clean_icon not in SERVER_TAG_ICONS:
        return {"ok": False, "error": "bad_icon"}
    await db.execute(text("""
        INSERT INTO community_server_boost_settings (server_id, tag_text, tag_icon, updated_at)
        VALUES (:server_id, :tag_text, :tag_icon, NOW())
        ON CONFLICT (server_id)
        DO UPDATE SET tag_text = EXCLUDED.tag_text,
                      tag_icon = EXCLUDED.tag_icon,
                      updated_at = NOW()
    """), {
        "server_id": int(server_id),
        "tag_text": clean_text,
        "tag_icon": clean_icon,
    })
    await db.commit()
    return {"ok": True, "tag": _server_tag_payload(clean_text, clean_icon, unlocked=True)}


async def set_account_server_tag(
    db: AsyncSession,
    account_id: int,
    server_id: int | None,
) -> dict:
    await ensure_server_boost_tables(db)
    if not server_id:
        await db.execute(text("""
            DELETE FROM community_account_server_tags WHERE account_id = :account_id
        """), {"account_id": int(account_id)})
        await db.commit()
        return {"ok": True, "tag": None, "server_id": None}
    if not await is_server_member(db, int(server_id), account_id):
        return {"ok": False, "error": "not_a_member"}
    status = await get_server_boost_status(db, int(server_id), account_id)
    if not status["tag"]["active"]:
        return {"ok": False, "error": "tag_unavailable"}
    await db.execute(text("""
        INSERT INTO community_account_server_tags (account_id, server_id, updated_at)
        VALUES (:account_id, :server_id, NOW())
        ON CONFLICT (account_id)
        DO UPDATE SET server_id = EXCLUDED.server_id, updated_at = NOW()
    """), {"account_id": int(account_id), "server_id": int(server_id)})
    await db.commit()
    return {"ok": True, "tag": status["tag"], "server_id": int(server_id)}


async def list_account_active_server_tags(
    db: AsyncSession,
    account_ids: list[int] | tuple[int, ...] | set[int],
) -> dict[int, dict]:
    await ensure_server_boost_tables(db)
    clean_ids = sorted({int(account_id) for account_id in account_ids if account_id})
    if not clean_ids:
        return {}
    rows = (await db.execute(text("""
        SELECT t.account_id, t.server_id, s.name AS server_name
        FROM community_account_server_tags t
        JOIN community_servers s ON s.id = t.server_id
        WHERE t.account_id = ANY(:account_ids)
    """), {"account_ids": clean_ids})).mappings().all()
    statuses: dict[int, dict] = {}
    for server_id in {int(row["server_id"]) for row in rows}:
        statuses[server_id] = await get_server_boost_status(db, server_id)
    result: dict[int, dict] = {}
    for row in rows:
        status = statuses.get(int(row["server_id"])) or {}
        tag = (status.get("tag") or {}).copy()
        if tag.get("active"):
            tag["server_id"] = int(row["server_id"])
            tag["server_name"] = row["server_name"]
            result[int(row["account_id"])] = tag
    return result


async def get_account_boost_center(db: AsyncSession, account_id: int) -> dict:
    await ensure_server_boost_tables(db)
    viewer = await reconcile_account_server_boosts(db, account_id)
    selected_server_id = (await db.execute(text("""
        SELECT server_id
        FROM community_account_server_tags
        WHERE account_id = :account_id
        LIMIT 1
    """), {"account_id": int(account_id)})).scalar_one_or_none()
    servers = await list_servers_for_account(db, account_id)
    server_payloads = []
    for server in servers:
        status = await get_server_boost_status(db, int(server.id), account_id)
        server_payloads.append({
            "id": int(server.id),
            "name": server.name,
            "icon_url": server.icon_url or "",
            "total_boosts": status["total_boosts"],
            "level": status["level"],
            "allocated_here": status["viewer"]["allocated_here"],
            "tag": status["tag"],
            "tag_selected": int(selected_server_id or 0) == int(server.id),
        })
    return {
        "ok": True,
        "subscription": viewer["subscription"],
        "capacity": int(viewer["capacity"]),
        "allocated": int(viewer["allocated"]),
        "remaining": int(viewer["remaining"]),
        "selected_tag_server_id": int(selected_server_id) if selected_server_id else None,
        "servers": server_payloads,
        "tier_boosts": dict(NITRO_TIER_BOOSTS),
        "tag_icons": [
            {"id": key, "glyph": glyph}
            for key, glyph in SERVER_TAG_ICONS.items()
        ],
    }


# ----------------------------------------------------------------------------
# Recipient-bound Nitro gifts in direct messages.
# ----------------------------------------------------------------------------
NITRO_DM_GIFT_MARKER_RE = re.compile(r"^\[\[ah:nitro-gift:([A-Za-z0-9_-]{20,96})\]\]$")
NITRO_DM_GIFT_DAYS = {7, 30, 90, 200}


def make_nitro_dm_gift_marker(public_token: str) -> str:
    return f"[[ah:nitro-gift:{public_token}]]"


def parse_nitro_dm_gift_marker(content: str | None) -> str | None:
    match = NITRO_DM_GIFT_MARKER_RE.fullmatch((content or "").strip())
    return match.group(1) if match else None


async def _generate_unique_nitro_dm_gift_token(db: AsyncSession) -> str:
    await ensure_nitro_tables(db)
    for _ in range(32):
        token = secrets.token_urlsafe(24).rstrip('=')
        exists = (await db.execute(text(
            "SELECT id FROM community_nitro_dm_gifts WHERE public_token = :token LIMIT 1"
        ), {"token": token})).first()
        if not exists:
            return token
    raise RuntimeError("Could not generate unique Nitro DM gift token")


def _nitro_dm_gift_payload(row, viewer_id: int) -> dict | None:
    if not row:
        return None
    status = str(row.get("status") or "pending")
    claimed_at = row.get("claimed_at")
    created_at = row.get("created_at")
    recipient_id = int(row["recipient_id"])
    sender_id = int(row["sender_id"])
    viewer_id = int(viewer_id)
    return {
        "token": row["public_token"],
        "thread_id": int(row["thread_id"]),
        "message_id": int(row["message_id"]) if row.get("message_id") is not None else None,
        "sender_id": sender_id,
        "sender_username": row.get("sender_username") or "user",
        "sender_avatar_url": row.get("sender_avatar_url") or "",
        "recipient_id": recipient_id,
        "recipient_username": row.get("recipient_username") or "user",
        "recipient_avatar_url": row.get("recipient_avatar_url") or "",
        "days": int(row.get("days") or 30),
        "note": row.get("note") or "",
        "status": status,
        "created_at": created_at.isoformat() if created_at else None,
        "claimed_at": claimed_at.isoformat() if claimed_at else None,
        "is_sender": viewer_id == sender_id,
        "is_recipient": viewer_id == recipient_id,
        "can_claim": viewer_id == recipient_id and status == "pending",
    }


async def get_nitro_dm_gift(db: AsyncSession, public_token: str, viewer_id: int) -> dict | None:
    await ensure_nitro_tables(db)
    token = (public_token or "").strip()
    if not token or len(token) > 96:
        return None
    row = (await db.execute(text("""
        SELECT g.public_token, g.thread_id, g.message_id, g.sender_id, g.recipient_id,
               g.days, g.note, g.status, g.created_at, g.claimed_at,
               sender.username AS sender_username, sender.avatar_url AS sender_avatar_url,
               recipient.username AS recipient_username, recipient.avatar_url AS recipient_avatar_url,
               thread.user_low_id, thread.user_high_id
        FROM community_nitro_dm_gifts AS g
        JOIN community_direct_threads AS thread ON thread.id = g.thread_id
        JOIN community_accounts AS sender ON sender.id = g.sender_id
        JOIN community_accounts AS recipient ON recipient.id = g.recipient_id
        WHERE g.public_token = :token
        LIMIT 1
    """), {"token": token})).mappings().first()
    if not row:
        return None
    if int(viewer_id) not in {int(row["user_low_id"]), int(row["user_high_id"])}:
        return None
    return _nitro_dm_gift_payload(row, int(viewer_id))


async def create_nitro_dm_gift(
    db: AsyncSession,
    thread_id: int,
    sender_id: int,
    recipient_id: int,
    days: int = 30,
    note: str | None = None,
) -> dict:
    await ensure_nitro_tables(db)
    await ensure_message_meta_columns(db)
    thread = await get_dm_thread_by_id(db, int(thread_id))
    if not thread:
        return {"ok": False, "error": "thread_not_found", "message": "ЛС не найдено."}
    participants = {int(thread.user_low_id), int(thread.user_high_id)}
    if int(sender_id) not in participants or int(recipient_id) not in participants or int(sender_id) == int(recipient_id):
        return {"ok": False, "error": "forbidden", "message": "Нельзя отправить подарок в это ЛС."}

    requested_days = int(days or 30)
    if requested_days not in NITRO_DM_GIFT_DAYS:
        requested_days = 30
    clean_note = (note or "").strip()[:160] or None
    token = await _generate_unique_nitro_dm_gift_token(db)
    marker = make_nitro_dm_gift_marker(token)

    try:
        inserted = (await db.execute(text("""
            INSERT INTO community_nitro_dm_gifts
                (public_token, thread_id, sender_id, recipient_id, days, note)
            VALUES
                (:token, :thread_id, :sender_id, :recipient_id, :days, :note)
            RETURNING id
        """), {
            "token": token,
            "thread_id": int(thread_id),
            "sender_id": int(sender_id),
            "recipient_id": int(recipient_id),
            "days": requested_days,
            "note": clean_note,
        })).first()
        if not inserted:
            await db.rollback()
            return {"ok": False, "error": "create_failed", "message": "Не удалось упаковать подарок."}

        # create_dm_message commits the pending gift row and the message together.
        message = await create_dm_message(db, int(thread_id), int(sender_id), marker)
        message_payload = {
            "id": int(message.id),
            "thread_id": int(thread_id),
            "author_id": int(sender_id),
            "content": marker,
            "image_url": None,
            "created_at": message.created_at.isoformat(),
            "reply_to_id": None,
            "reply": None,
            "is_forwarded": False,
        }
        await db.execute(text("""
            UPDATE community_nitro_dm_gifts
            SET message_id = :message_id
            WHERE public_token = :token
        """), {"message_id": int(message.id), "token": token})
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    gift = await get_nitro_dm_gift(db, token, int(sender_id))
    return {"ok": True, "gift": gift, "message": message_payload}


async def claim_nitro_dm_gift(db: AsyncSession, public_token: str, account_id: int) -> dict:
    await ensure_nitro_tables(db)
    token = (public_token or "").strip()
    if not token or len(token) > 96:
        return {"ok": False, "error": "not_found", "message": "Подарок не найден."}

    row = (await db.execute(text("""
        SELECT id, public_token, thread_id, message_id, sender_id, recipient_id, days, status
        FROM community_nitro_dm_gifts
        WHERE public_token = :token
        LIMIT 1
        FOR UPDATE
    """), {"token": token})).mappings().first()
    if not row:
        await db.rollback()
        return {"ok": False, "error": "not_found", "message": "Подарок не найден."}
    if int(row["recipient_id"]) != int(account_id):
        await db.rollback()
        return {"ok": False, "error": "not_recipient", "message": "Этот подарок предназначен другому пользователю."}

    if str(row.get("status") or "pending") == "claimed":
        await db.rollback()
        gift = await get_nitro_dm_gift(db, token, int(account_id))
        if gift:
            gift["sender_gifting"] = await get_nitro_gifter_badge(db, int(row["sender_id"]))
        sub = await get_nitro_subscription(db, int(account_id))
        return {"ok": True, "already_claimed": True, "gift": gift, "subscription": sub}

    now = datetime.now(timezone.utc)
    current = (await db.execute(text("""
        SELECT started_at, expires_at, source_code
        FROM community_nitro_subscriptions
        WHERE account_id = :account_id
        LIMIT 1
        FOR UPDATE
    """), {"account_id": int(account_id)})).mappings().first()

    current_expires_at = _nitro_datetime_utc(current["expires_at"]) if current else None
    current_started_at = _nitro_datetime_utc(current["started_at"]) if current else None
    current_is_active = bool(current_expires_at and current_expires_at > now)
    started_at = current_started_at if current_is_active and current_started_at else now
    base_time = current_expires_at if current_is_active else now
    days = max(1, min(int(row.get("days") or 30), 365))
    expires_at = base_time + timedelta(days=days)
    source_code = f"DMGIFT-{token}"[:64]

    if current:
        await db.execute(text("""
            UPDATE community_nitro_subscriptions
            SET started_at = :started_at, expires_at = :expires_at,
                source_code = :source_code, updated_at = NOW()
            WHERE account_id = :account_id
        """), {
            "started_at": started_at,
            "expires_at": expires_at,
            "source_code": source_code,
            "account_id": int(account_id),
        })
    else:
        await db.execute(text("""
            INSERT INTO community_nitro_subscriptions
                (account_id, started_at, expires_at, source_code)
            VALUES
                (:account_id, :started_at, :expires_at, :source_code)
        """), {
            "account_id": int(account_id),
            "started_at": started_at,
            "expires_at": expires_at,
            "source_code": source_code,
        })

    await db.execute(text("""
        UPDATE community_nitro_dm_gifts
        SET status = 'claimed', claimed_at = NOW()
        WHERE id = :gift_id AND status = 'pending'
    """), {"gift_id": int(row["id"])})
    await db.commit()

    gift = await get_nitro_dm_gift(db, token, int(account_id))
    if gift:
        gift["sender_gifting"] = await get_nitro_gifter_badge(db, int(row["sender_id"]))
    sub = await get_nitro_subscription(db, int(account_id))
    return {"ok": True, "days_added": days, "gift": gift, "subscription": sub}


# ============================================================================
# Custom server emojis + stickers.
# ============================================================================
_SERVER_MEDIA_TABLES_READY = False

def _media_dt(v):
    return v.isoformat() if v else None

def _clean_media_name(name: str | None) -> str:
    raw = (name or '').strip().lower()
    safe = ''.join(ch if (ch.isalnum() or ch in ['_', '-']) else '_' for ch in raw)
    safe = '_'.join(part for part in safe.split('_') if part)
    return (safe or 'media')[:32]

async def ensure_server_media_tables(db: AsyncSession) -> None:
    global _SERVER_MEDIA_TABLES_READY
    if _SERVER_MEDIA_TABLES_READY:
        return
    await db.execute(text('''
        CREATE TABLE IF NOT EXISTS community_server_emojis (
            id SERIAL PRIMARY KEY,
            server_id INTEGER NOT NULL REFERENCES community_servers(id) ON DELETE CASCADE,
            name VARCHAR(64) NOT NULL,
            image_url TEXT NOT NULL,
            content_type VARCHAR(80),
            created_by_id INTEGER REFERENCES community_accounts(id) ON DELETE SET NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    '''))
    await db.execute(text('''
        CREATE UNIQUE INDEX IF NOT EXISTS uq_community_server_emojis_server_name
        ON community_server_emojis (server_id, lower(name))
    '''))
    await db.execute(text('''
        CREATE TABLE IF NOT EXISTS community_server_stickers (
            id SERIAL PRIMARY KEY,
            server_id INTEGER NOT NULL REFERENCES community_servers(id) ON DELETE CASCADE,
            name VARCHAR(64) NOT NULL,
            description TEXT,
            emoji VARCHAR(32),
            image_url TEXT NOT NULL,
            content_type VARCHAR(80),
            created_by_id INTEGER REFERENCES community_accounts(id) ON DELETE SET NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    '''))
    await db.execute(text('''
        CREATE UNIQUE INDEX IF NOT EXISTS uq_community_server_stickers_server_name
        ON community_server_stickers (server_id, lower(name))
    '''))
    await db.commit()
    _SERVER_MEDIA_TABLES_READY = True

def _emoji_payload(row, *, allowed: bool = True, local: bool = True) -> dict:
    return {'id': int(row['id']), 'server_id': int(row['server_id']), 'name': row['name'], 'shortcode': ':' + row['name'] + ':', 'image_url': row['image_url'], 'content_type': row.get('content_type') if hasattr(row,'get') else row['content_type'], 'created_at': _media_dt(row.get('created_at') if hasattr(row,'get') else row['created_at']), 'allowed': bool(allowed), 'local': bool(local), 'type': 'emoji'}

def _sticker_payload(row, *, allowed: bool = True, local: bool = True) -> dict:
    return {'id': int(row['id']), 'server_id': int(row['server_id']), 'name': row['name'], 'description': row.get('description') if hasattr(row,'get') else row['description'], 'emoji': row.get('emoji') if hasattr(row,'get') else row['emoji'], 'image_url': row['image_url'], 'content_type': row.get('content_type') if hasattr(row,'get') else row['content_type'], 'created_at': _media_dt(row.get('created_at') if hasattr(row,'get') else row['created_at']), 'allowed': bool(allowed), 'local': bool(local), 'type': 'sticker'}

async def list_server_emojis(db: AsyncSession, server_id: int) -> list[dict]:
    await ensure_server_media_tables(db)
    rows=(await db.execute(text('''SELECT id, server_id, name, image_url, content_type, created_at FROM community_server_emojis WHERE server_id=:server_id ORDER BY created_at DESC,id DESC'''), {'server_id':int(server_id)})).mappings().all()
    return [_emoji_payload(r) for r in rows]

async def list_server_stickers(db: AsyncSession, server_id: int) -> list[dict]:
    await ensure_server_media_tables(db)
    rows=(await db.execute(text('''SELECT id, server_id, name, description, emoji, image_url, content_type, created_at FROM community_server_stickers WHERE server_id=:server_id ORDER BY created_at DESC,id DESC'''), {'server_id':int(server_id)})).mappings().all()
    return [_sticker_payload(r) for r in rows]

async def create_server_emoji(db: AsyncSession, server_id: int, name: str, image_url: str, content_type: str | None, created_by_id: int) -> dict:
    await ensure_server_media_tables(db); clean=_clean_media_name(name)
    await db.execute(text('''INSERT INTO community_server_emojis(server_id,name,image_url,content_type,created_by_id) VALUES(:server_id,:name,:image_url,:content_type,:created_by_id) ON CONFLICT (server_id, lower(name)) DO UPDATE SET image_url=EXCLUDED.image_url, content_type=EXCLUDED.content_type, created_by_id=EXCLUDED.created_by_id, created_at=NOW()'''), {'server_id':int(server_id),'name':clean,'image_url':image_url,'content_type':content_type,'created_by_id':int(created_by_id)})
    await db.commit()
    row=(await db.execute(text('''SELECT id,server_id,name,image_url,content_type,created_at FROM community_server_emojis WHERE server_id=:server_id AND lower(name)=lower(:name) LIMIT 1'''), {'server_id':int(server_id),'name':clean})).mappings().first()
    return _emoji_payload(row)

async def create_server_sticker(db: AsyncSession, server_id: int, name: str, description: str | None, emoji: str | None, image_url: str, content_type: str | None, created_by_id: int) -> dict:
    await ensure_server_media_tables(db); clean=_clean_media_name(name)
    await db.execute(text('''INSERT INTO community_server_stickers(server_id,name,description,emoji,image_url,content_type,created_by_id) VALUES(:server_id,:name,:description,:emoji,:image_url,:content_type,:created_by_id) ON CONFLICT (server_id, lower(name)) DO UPDATE SET description=EXCLUDED.description, emoji=EXCLUDED.emoji, image_url=EXCLUDED.image_url, content_type=EXCLUDED.content_type, created_by_id=EXCLUDED.created_by_id, created_at=NOW()'''), {'server_id':int(server_id),'name':clean,'description':(description or '').strip()[:240] or None,'emoji':(emoji or '').strip()[:24] or None,'image_url':image_url,'content_type':content_type,'created_by_id':int(created_by_id)})
    await db.commit()
    row=(await db.execute(text('''SELECT id,server_id,name,description,emoji,image_url,content_type,created_at FROM community_server_stickers WHERE server_id=:server_id AND lower(name)=lower(:name) LIMIT 1'''), {'server_id':int(server_id),'name':clean})).mappings().first()
    return _sticker_payload(row)

async def delete_server_emoji(db: AsyncSession, server_id: int, emoji_id: int) -> bool:
    await ensure_server_media_tables(db)
    row=(await db.execute(text('''DELETE FROM community_server_emojis WHERE server_id=:server_id AND id=:id RETURNING id'''), {'server_id':int(server_id),'id':int(emoji_id)})).first(); await db.commit(); return bool(row)

async def delete_server_sticker(db: AsyncSession, server_id: int, sticker_id: int) -> bool:
    await ensure_server_media_tables(db)
    row=(await db.execute(text('''DELETE FROM community_server_stickers WHERE server_id=:server_id AND id=:id RETURNING id'''), {'server_id':int(server_id),'id':int(sticker_id)})).first(); await db.commit(); return bool(row)

async def media_library_for_account(db: AsyncSession, account_id: int, current_server_id: int | None = None, context: str = 'server') -> dict:
    await ensure_server_media_tables(db)
    sub=await get_nitro_subscription(db, account_id); nitro=bool(sub.get('active'))
    servers=await list_servers_for_account(db, account_id); ids=[int(s.id) for s in servers]
    if not ids: return {'nitro':nitro,'emojis':[],'stickers':[]}
    rows_e=(await db.execute(text('''SELECT e.id,e.server_id,e.name,e.image_url,e.content_type,e.created_at,s.name AS server_name,s.icon_url AS server_icon_url FROM community_server_emojis e JOIN community_servers s ON s.id=e.server_id WHERE e.server_id = ANY(:ids) ORDER BY s.name ASC,e.created_at DESC'''), {'ids':ids})).mappings().all()
    rows_s=(await db.execute(text('''SELECT st.id,st.server_id,st.name,st.description,st.emoji,st.image_url,st.content_type,st.created_at,s.name AS server_name,s.icon_url AS server_icon_url FROM community_server_stickers st JOIN community_servers s ON s.id=st.server_id WHERE st.server_id = ANY(:ids) ORDER BY s.name ASC,st.created_at DESC'''), {'ids':ids})).mappings().all()
    def allow(sid): return bool((context=='server' and current_server_id and int(sid)==int(current_server_id)) or nitro)
    emojis=[]
    for r in rows_e:
        it=_emoji_payload(r, allowed=allow(r['server_id']), local=bool(current_server_id and int(r['server_id'])==int(current_server_id))); it['server_name']=r['server_name']; it['server_icon_url']=r['server_icon_url']; emojis.append(it)
    stickers=[]
    for r in rows_s:
        it=_sticker_payload(r, allowed=allow(r['server_id']), local=bool(current_server_id and int(r['server_id'])==int(current_server_id))); it['server_name']=r['server_name']; it['server_icon_url']=r['server_icon_url']; stickers.append(it)
    return {'nitro':nitro,'emojis':emojis,'stickers':stickers}


_CUSTOM_EMOJI_MARKER_RE = re.compile(r"\[\[ah:emoji:(\d+)\]\]")


async def list_referenced_message_emojis(
    db: AsyncSession,
    *,
    context: str,
    thread_id: int | None = None,
    server_id: int | None = None,
    channel_id: int | None = None,
) -> list[dict]:
    """Resolve emoji IDs already stored in a chat so every viewer can render them.

    These records are display-only. Sending still goes through
    ``get_media_item_for_send`` and its membership/Nitro checks.
    """
    await ensure_server_media_tables(db)
    if context == "dm" and thread_id:
        result = await db.execute(
            select(DirectMessage.content)
            .where(
                DirectMessage.thread_id == int(thread_id),
                DirectMessage.content.contains("[[ah:emoji:"),
            )
            .order_by(DirectMessage.id.desc())
            .limit(2000)
        )
    elif context == "server" and server_id and channel_id:
        result = await db.execute(
            select(ServerMessage.content)
            .where(
                ServerMessage.server_id == int(server_id),
                ServerMessage.channel_id == int(channel_id),
                ServerMessage.content.contains("[[ah:emoji:"),
            )
            .order_by(ServerMessage.id.desc())
            .limit(2000)
        )
    else:
        return []

    emoji_ids: set[int] = set()
    for content in result.scalars().all():
        for match in _CUSTOM_EMOJI_MARKER_RE.finditer(content or ""):
            emoji_ids.add(int(match.group(1)))
            if len(emoji_ids) >= 500:
                break
        if len(emoji_ids) >= 500:
            break
    if not emoji_ids:
        return []

    rows = (
        await db.execute(
            text(
                """
                SELECT e.id, e.server_id, e.name, e.image_url, e.content_type,
                       e.created_at, s.name AS server_name,
                       s.icon_url AS server_icon_url
                FROM community_server_emojis e
                JOIN community_servers s ON s.id = e.server_id
                WHERE e.id = ANY(:ids)
                ORDER BY e.id ASC
                """
            ),
            {"ids": sorted(emoji_ids)},
        )
    ).mappings().all()
    items: list[dict] = []
    for row in rows:
        item = _emoji_payload(row, allowed=False, local=False)
        item["server_name"] = row["server_name"]
        item["server_icon_url"] = row["server_icon_url"]
        item["reference_only"] = True
        items.append(item)
    return items


async def get_media_item_for_send(
    db: AsyncSession,
    account_id: int,
    kind: str,
    item_id: int,
    current_server_id: int | None = None,
    context: str = 'server',
) -> dict | None:
    """Resolve a custom emoji/sticker and enforce the Discord-like Nitro rule.

    Rule:
    - inside the same server where the media was created: allowed without Nitro;
    - in DM or another server: allowed only with active Nitro;
    - user must still be a member of the source server, so random ids cannot be abused.
    """
    await ensure_server_media_tables(db)
    if kind == 'sticker':
        row = (await db.execute(text("""
            SELECT st.id, st.server_id, st.name, st.description, st.emoji, st.image_url, st.content_type, st.created_at,
                   s.name AS server_name, s.icon_url AS server_icon_url
            FROM community_server_stickers st
            JOIN community_servers s ON s.id = st.server_id
            WHERE st.id = :id
            LIMIT 1
        """), {'id': int(item_id)})).mappings().first()
    else:
        row = (await db.execute(text("""
            SELECT e.id, e.server_id, e.name, e.image_url, e.content_type, e.created_at,
                   s.name AS server_name, s.icon_url AS server_icon_url
            FROM community_server_emojis e
            JOIN community_servers s ON s.id = e.server_id
            WHERE e.id = :id
            LIMIT 1
        """), {'id': int(item_id)})).mappings().first()
    if not row:
        return None
    if not await is_server_member(db, int(row['server_id']), int(account_id)):
        return None
    sub = await get_nitro_subscription(db, int(account_id))
    has_nitro = bool(sub.get('active'))
    local = bool(context == 'server' and current_server_id and int(current_server_id) == int(row['server_id']))
    allowed = bool(local or has_nitro)
    payload = _sticker_payload(row, allowed=allowed, local=local) if kind == 'sticker' else _emoji_payload(row, allowed=allowed, local=local)
    payload['server_name'] = row.get('server_name') if hasattr(row, 'get') else row['server_name']
    payload['server_icon_url'] = row.get('server_icon_url') if hasattr(row, 'get') else row['server_icon_url']
    payload['nitro'] = has_nitro
    return payload

# ============================================================================
# Message reactions (DM + server channels)
# ============================================================================

_REACTION_TABLES_READY = False
REACTION_UNICODE_EMOJIS = {
    "❤️", "💛", "💚", "💙", "💜", "🩷", "🖤", "🤍",
    "👍", "👎", "👏", "🙏", "💯", "🔥", "⭐", "✨",
    "😂", "🤣", "😭", "🥹", "😍", "🥰", "😎", "🤔",
    "😡", "🤯", "😱", "😴", "🤡", "💀", "👀", "🫡",
    "✅", "❌", "🎉", "🎄", "🎁", "💩", "🫶", "🤝",
    "🇺🇦", "🗿", "🐸", "🫠", "😈", "🥳", "🤍", "💔",
}


async def ensure_reaction_tables(db: AsyncSession) -> None:
    """Create reaction storage without requiring an Alembic migration."""
    global _REACTION_TABLES_READY
    if _REACTION_TABLES_READY:
        return
    # The custom emoji FK target must exist first.
    await ensure_server_media_tables(db)
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS community_message_reactions (
            id BIGSERIAL PRIMARY KEY,
            context VARCHAR(16) NOT NULL,
            message_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL REFERENCES community_accounts(id) ON DELETE CASCADE,
            emoji_kind VARCHAR(16) NOT NULL,
            emoji_value VARCHAR(64),
            custom_emoji_id INTEGER REFERENCES community_server_emojis(id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_community_message_reaction_actor
        ON community_message_reactions (
            context,
            message_id,
            account_id,
            emoji_kind,
            COALESCE(custom_emoji_id, 0),
            COALESCE(emoji_value, '')
        )
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_community_message_reactions_message
        ON community_message_reactions (context, message_id, created_at)
    """))
    await db.commit()
    _REACTION_TABLES_READY = True


def _reaction_clean_ids(message_ids: list[int] | tuple[int, ...] | None) -> list[int]:
    clean: list[int] = []
    seen: set[int] = set()
    for raw in message_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        clean.append(value)
        seen.add(value)
        if len(clean) >= 100:
            break
    return clean


async def _valid_dm_reaction_message_ids(db: AsyncSession, thread_id: int, message_ids: list[int]) -> list[int]:
    ids = _reaction_clean_ids(message_ids)
    if not ids:
        return []
    rows = (await db.execute(
        select(DirectMessage.id).where(
            DirectMessage.thread_id == int(thread_id),
            DirectMessage.id.in_(ids),
        )
    )).scalars().all()
    return [int(value) for value in rows]


async def _valid_server_reaction_message_ids(
    db: AsyncSession,
    server_id: int,
    channel_id: int,
    message_ids: list[int],
) -> list[int]:
    ids = _reaction_clean_ids(message_ids)
    if not ids:
        return []
    rows = (await db.execute(
        select(ServerMessage.id).where(
            ServerMessage.server_id == int(server_id),
            ServerMessage.channel_id == int(channel_id),
            ServerMessage.id.in_(ids),
        )
    )).scalars().all()
    return [int(value) for value in rows]


def _reaction_key(row) -> str:
    if str(row["emoji_kind"]) == "custom":
        return f"custom:{int(row['custom_emoji_id'])}"
    return "unicode:" + str(row["emoji_value"] or "")


async def _reaction_summaries(
    db: AsyncSession,
    *,
    context: str,
    message_ids: list[int],
    viewer_id: int,
) -> dict[int, list[dict]]:
    await ensure_reaction_tables(db)
    ids = _reaction_clean_ids(message_ids)
    if not ids:
        return {}
    rows = (await db.execute(text("""
        SELECT
            r.message_id,
            r.account_id,
            r.emoji_kind,
            r.emoji_value,
            r.custom_emoji_id,
            r.created_at,
            a.username,
            a.avatar_url,
            e.name AS custom_name,
            e.image_url AS custom_image_url,
            e.server_id AS custom_server_id
        FROM community_message_reactions AS r
        JOIN community_accounts AS a ON a.id = r.account_id
        LEFT JOIN community_server_emojis AS e ON e.id = r.custom_emoji_id
        WHERE r.context = :context
          AND r.message_id = ANY(:message_ids)
        ORDER BY r.message_id ASC, r.created_at ASC, r.id ASC
    """), {"context": context, "message_ids": ids})).mappings().all()

    grouped: dict[int, dict[str, dict]] = {}
    for row in rows:
        message_id = int(row["message_id"])
        key = _reaction_key(row)
        message_group = grouped.setdefault(message_id, {})
        item = message_group.get(key)
        if item is None:
            is_custom = str(row["emoji_kind"]) == "custom"
            item = {
                "key": key,
                "kind": "custom" if is_custom else "unicode",
                "value": None if is_custom else (row["emoji_value"] or ""),
                "custom_emoji_id": int(row["custom_emoji_id"]) if is_custom and row["custom_emoji_id"] else None,
                "name": (row["custom_name"] or "emoji") if is_custom else (row["emoji_value"] or "emoji"),
                "image_url": (row["custom_image_url"] or "") if is_custom else "",
                "server_id": int(row["custom_server_id"]) if is_custom and row["custom_server_id"] else None,
                "count": 0,
                "me": False,
                "users": [],
            }
            message_group[key] = item
        account_id = int(row["account_id"])
        item["count"] += 1
        item["me"] = bool(item["me"] or account_id == int(viewer_id))
        item["users"].append({
            "id": account_id,
            "username": row["username"] or "user",
            "avatar_url": row["avatar_url"] or "",
        })

    return {message_id: list(items.values()) for message_id, items in grouped.items()}


async def list_dm_reaction_summaries(
    db: AsyncSession,
    thread_id: int,
    message_ids: list[int],
    viewer_id: int,
) -> dict[int, list[dict]]:
    valid_ids = await _valid_dm_reaction_message_ids(db, thread_id, message_ids)
    return await _reaction_summaries(
        db,
        context="dm",
        message_ids=valid_ids,
        viewer_id=viewer_id,
    )


async def list_server_reaction_summaries(
    db: AsyncSession,
    server_id: int,
    channel_id: int,
    message_ids: list[int],
    viewer_id: int,
) -> dict[int, list[dict]]:
    valid_ids = await _valid_server_reaction_message_ids(db, server_id, channel_id, message_ids)
    return await _reaction_summaries(
        db,
        context="server",
        message_ids=valid_ids,
        viewer_id=viewer_id,
    )


async def toggle_message_reaction(
    db: AsyncSession,
    *,
    context: str,
    message_id: int,
    account_id: int,
    emoji_kind: str,
    emoji_value: str | None = None,
    custom_emoji_id: int | None = None,
    current_server_id: int | None = None,
) -> dict:
    """Toggle one reaction and return a fresh aggregate for the message.

    Existing reactions are removable even after Nitro expires. Adding an emoji
    from another server still goes through the same Nitro/source-membership rule
    as sending that emoji in a message.
    """
    await ensure_reaction_tables(db)
    clean_context = "server" if context == "server" else "dm"
    clean_kind = "custom" if emoji_kind == "custom" else "unicode"
    clean_value = (emoji_value or "").strip()
    custom_id = int(custom_emoji_id or 0) if clean_kind == "custom" else 0

    if clean_kind == "custom" and custom_id <= 0:
        return {"ok": False, "error": "bad_emoji"}
    if clean_kind == "unicode" and clean_value not in REACTION_UNICODE_EMOJIS:
        return {"ok": False, "error": "bad_emoji"}

    existing = (await db.execute(text("""
        SELECT id
        FROM community_message_reactions
        WHERE context = :context
          AND message_id = :message_id
          AND account_id = :account_id
          AND emoji_kind = :emoji_kind
          AND COALESCE(custom_emoji_id, 0) = :custom_emoji_id
          AND COALESCE(emoji_value, '') = :emoji_value
        LIMIT 1
    """), {
        "context": clean_context,
        "message_id": int(message_id),
        "account_id": int(account_id),
        "emoji_kind": clean_kind,
        "custom_emoji_id": custom_id,
        "emoji_value": clean_value if clean_kind == "unicode" else "",
    })).mappings().first()

    if existing:
        await db.execute(text("DELETE FROM community_message_reactions WHERE id = :id"), {"id": int(existing["id"])})
        await db.commit()
        summary = await _reaction_summaries(
            db,
            context=clean_context,
            message_ids=[int(message_id)],
            viewer_id=int(account_id),
        )
        return {"ok": True, "added": False, "reactions": summary.get(int(message_id), [])}

    if clean_kind == "custom":
        item = await get_media_item_for_send(
            db,
            account_id=int(account_id),
            kind="emoji",
            item_id=custom_id,
            current_server_id=current_server_id if clean_context == "server" else None,
            context=clean_context,
        )
        if not item:
            return {"ok": False, "error": "emoji_unavailable"}
        if not item.get("allowed"):
            return {"ok": False, "error": "nitro_required"}

    await db.execute(text("""
        INSERT INTO community_message_reactions
            (context, message_id, account_id, emoji_kind, emoji_value, custom_emoji_id)
        VALUES
            (:context, :message_id, :account_id, :emoji_kind, :emoji_value, :custom_emoji_id)
        ON CONFLICT DO NOTHING
    """), {
        "context": clean_context,
        "message_id": int(message_id),
        "account_id": int(account_id),
        "emoji_kind": clean_kind,
        "emoji_value": clean_value if clean_kind == "unicode" else None,
        "custom_emoji_id": custom_id if clean_kind == "custom" else None,
    })
    await db.commit()
    summary = await _reaction_summaries(
        db,
        context=clean_context,
        message_ids=[int(message_id)],
        viewer_id=int(account_id),
    )
    return {"ok": True, "added": True, "reactions": summary.get(int(message_id), [])}
