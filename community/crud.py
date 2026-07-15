"""
Query functions for the community module -- kept separate from the admin
dashboard's crud.py so the two stay easy to reason about independently.
"""
from datetime import datetime, timedelta, timezone
import re
import secrets

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    ServerInvite,
    ServerMember,
    ServerMessage,
)

# A member counts as "online" if we've seen a request from them in the
# last 3 minutes. Cheap to compute, no background job or websocket needed.
ONLINE_WINDOW = timedelta(minutes=3)

VISUAL_NAME_EFFECTS = {"none", "gradient", "glow"}
VISUAL_NAME_FONTS = {"default", "mono", "serif", "rounded", "cyber", "display", "pixel", "bubble", "puffy", "block", "neon", "glitch", "graffiti", "spooky", "medieval", "roundfat"}

PRESENCE_STATUSES = {"online", "idle", "dnd", "invisible"}
SUPPORTED_LANGUAGES = {"ru", "uk", "en"}
DEFAULT_LANGUAGE = "ru"


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
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_effect VARCHAR(32)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_color_start VARCHAR(16)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_color_end VARCHAR(16)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_font VARCHAR(32)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS profile_card_bg_url VARCHAR(512)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS account_status VARCHAR(16) DEFAULT 'online' NOT NULL"))
    await db.execute(text("UPDATE community_accounts SET account_status = 'online' WHERE account_status IS NULL OR account_status = ''"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS language VARCHAR(8) DEFAULT 'ru' NOT NULL"))
    await db.execute(text("UPDATE community_accounts SET language = 'ru' WHERE language IS NULL OR language = ''"))
    await db.commit()


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


async def get_server_by_id(db: AsyncSession, server_id: int) -> CommunityServer | None:
    result = await db.execute(select(CommunityServer).where(CommunityServer.id == server_id))
    return result.scalar_one_or_none()


async def get_server_member(db: AsyncSession, server_id: int, account_id: int) -> ServerMember | None:
    result = await db.execute(
        select(ServerMember).where(ServerMember.server_id == server_id, ServerMember.account_id == account_id)
    )
    return result.scalar_one_or_none()


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
    invite.status = "accepted"
    invite.is_used = True
    invite.responded_at = datetime.now(timezone.utc)

    if not await is_server_member(db, server_id, account_id):
        db.add(ServerMember(server_id=server_id, account_id=account_id, role="member"))

    await db.commit()
    await db.refresh(invite)
    return invite


async def can_manage_server(db: AsyncSession, server_id: int, account_id: int) -> bool:
    member = await get_server_member(db, server_id, account_id)
    return bool(member and member.role in {"owner", "admin"})


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
    server = await get_server_by_id(db, server_id)
    if not server:
        return False

    for model, column in [
        (ServerMessage, ServerMessage.server_id),
        (ServerInvite, ServerInvite.server_id),
        (ServerMember, ServerMember.server_id),
        (ServerChannel, ServerChannel.server_id),
    ]:
        result = await db.execute(select(model).where(column == server_id))
        for row in result.scalars().all():
            await db.delete(row)

    await db.delete(server)
    await db.commit()
    return True


async def list_server_channels(db: AsyncSession, server_id: int) -> list[ServerChannel]:
    result = await db.execute(
        select(ServerChannel).where(ServerChannel.server_id == server_id).order_by(ServerChannel.sort_order.asc(), ServerChannel.id.asc())
    )
    return list(result.scalars().all())


async def get_server_channel(db: AsyncSession, server_id: int, channel_id: int) -> ServerChannel | None:
    result = await db.execute(
        select(ServerChannel).where(ServerChannel.server_id == server_id, ServerChannel.id == channel_id)
    )
    return result.scalar_one_or_none()


async def create_server_channel(
    db: AsyncSession,
    server_id: int,
    name: str,
    description: str | None = None,
) -> ServerChannel:
    clean_name = name.strip().lower().replace(" ", "-")[:64]
    if not clean_name:
        clean_name = "new-channel"
    count_result = await db.execute(select(func.count()).select_from(ServerChannel).where(ServerChannel.server_id == server_id))
    order = int(count_result.scalar_one() or 0)
    channel = ServerChannel(
        server_id=server_id,
        name=clean_name,
        description=description.strip()[:255] if description else None,
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
        select(ServerMember).where(ServerMember.server_id == server_id).order_by(ServerMember.role.asc(), ServerMember.joined_at.asc())
    )
    members = []
    for member in result.scalars().all():
        account = await get_account_by_id(db, member.account_id)
        members.append({"member": member, "account": account})
    return members


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

    Important: the current Account model does NOT always have display_name.
    The previous version used friend.display_name directly and could throw
    AttributeError, which made /api/forward-targets return 500 and the modal
    show "Не удалось загрузить список".
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
}


def nitro_tier_from_duration(duration_days: int | float | None) -> tuple[str, str]:
    """Return one canonical Nitro tier for the full uninterrupted credit period."""
    days = max(0, int(duration_days or 0))
    if days >= 200:
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


async def nitro_profile_payload(db: AsyncSession, account_id: int) -> dict:
    sub = await get_nitro_subscription(db, account_id)
    return {
        "active": bool(sub.get('active')),
        "started_at": sub.get('started_at'),
        "expires_at": sub.get('expires_at'),
        "days_left": sub.get('days_left') or 0,
        "duration_days": sub.get('duration_days') or 0,
        "tier": sub.get('tier') or 'basic',
        "tier_label": sub.get('tier_label') or NITRO_TIER_LABELS['basic'],
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
