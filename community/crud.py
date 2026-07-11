"""
Query functions for the community module -- kept separate from the admin
dashboard's crud.py so the two stay easy to reason about independently.
"""
from datetime import datetime, timedelta, timezone
import re

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
    await db.commit()


# Server invite safety guard.
# Render/create_all() does not add columns to old tables, so every route that touches
# invites can call this cheaply. It only does real work once per process.
_SERVER_INVITE_COLUMNS_READY = False

async def ensure_server_invite_columns(db: AsyncSession) -> None:
    global _SERVER_INVITE_COLUMNS_READY
    if _SERVER_INVITE_COLUMNS_READY:
        return
    await db.execute(text("ALTER TABLE community_server_invites ADD COLUMN IF NOT EXISTS status VARCHAR(16) DEFAULT 'pending' NOT NULL"))
    await db.execute(text("ALTER TABLE community_server_invites ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL"))
    await db.execute(text("ALTER TABLE community_server_invites ADD COLUMN IF NOT EXISTS responded_at TIMESTAMP WITH TIME ZONE"))
    await db.execute(text("UPDATE community_server_invites SET status='pending' WHERE status IS NULL OR status=''"))
    await db.commit()
    _SERVER_INVITE_COLUMNS_READY = True


SERVER_INVITE_DM_PREFIX = "alexihub://server-invite/"


def make_server_invite_dm_content(invite_id: int, channel_id: int | None = None) -> str:
    base = f"{SERVER_INVITE_DM_PREFIX}{int(invite_id)}"
    if channel_id:
        return f"{base}?channel={int(channel_id)}"
    return base


def parse_server_invite_dm_content(content: str | None) -> tuple[int, int | None] | None:
    raw = (content or "").strip()
    if not raw.startswith(SERVER_INVITE_DM_PREFIX):
        return None
    m = re.search(r"alexihub://server-invite/(\d+)(?:\?channel=(\d+))?", raw)
    if not m:
        return None
    invite_id = int(m.group(1))
    channel_id = int(m.group(2)) if m.group(2) else None
    return invite_id, channel_id




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
    """Join a server by numeric invite/server id. Used by the Discord-like join modal.

    This keeps the DB load tiny: one server lookup, one membership lookup,
    and one insert only when the user is not already a member.
    """
    server = await get_server_by_id(db, server_id)
    if not server:
        return None

    existing = await get_server_member(db, server_id, account_id)
    if not existing:
        db.add(ServerMember(server_id=server_id, account_id=account_id, role="member"))
        await db.commit()

    return server


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
    if not await is_server_member(db, server_id, invitee_id):
        # ok, target is not in server yet
        pass
    else:
        return None

    # Only friends can be invited, keeps random spam out.
    if await friendship_status(db, inviter_id, invitee_id) != "friends":
        return None

    result = await db.execute(
        select(ServerInvite).where(ServerInvite.server_id == server_id, ServerInvite.invitee_id == invitee_id)
    )
    existing = result.scalar_one_or_none()
    if existing and existing.status == "pending":
        return existing
    if existing and existing.status in {"declined", "accepted"}:
        existing.inviter_id = inviter_id
        existing.status = "pending"
        existing.responded_at = None
        await db.commit()
        await db.refresh(existing)
        return existing

    invite = ServerInvite(server_id=server_id, inviter_id=inviter_id, invitee_id=invitee_id, status="pending")
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
    invite_id: int,
    account_id: int,
    accept: bool,
) -> ServerInvite | None:
    await ensure_server_invite_columns(db)
    result = await db.execute(select(ServerInvite).where(ServerInvite.id == invite_id))
    invite = result.scalar_one_or_none()
    if not invite or invite.invitee_id != account_id or invite.status != "pending":
        return None

    invite.status = "accepted" if accept else "declined"
    invite.responded_at = datetime.now(timezone.utc)

    if accept and not await is_server_member(db, invite.server_id, account_id):
        db.add(ServerMember(server_id=invite.server_id, account_id=account_id, role="member"))

    await db.commit()
    await db.refresh(invite)
    return invite


async def get_server_invite_by_id(db: AsyncSession, invite_id: int) -> ServerInvite | None:
    await ensure_server_invite_columns(db)
    result = await db.execute(select(ServerInvite).where(ServerInvite.id == invite_id))
    return result.scalar_one_or_none()


async def build_server_invite_preview(
    db: AsyncSession,
    invite_id: int,
    viewer_id: int,
    channel_id: int | None = None,
) -> dict:
    """Return a plain JSON-safe invite preview.

    This intentionally returns a dict instead of ORM objects so the template never
    touches lazy/expired SQLAlchemy state. One broken invite must not break DM view.
    """
    await ensure_server_invite_columns(db)
    try:
        invite = await get_server_invite_by_id(db, invite_id)
        if not invite:
            return {"type": "server_invite", "invite_id": invite_id, "valid": False, "status": "invalid"}

        server = await get_server_by_id(db, invite.server_id)
        if not server:
            return {"type": "server_invite", "invite_id": invite_id, "valid": False, "status": "invalid"}

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
        valid = invite.status == "pending" and (viewer_is_invitee or viewer_is_inviter)

        return {
            "type": "server_invite",
            "invite_id": int(invite.id),
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
            "valid": bool(valid),
            "members_count": members_count,
        }
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        return {"type": "server_invite", "invite_id": invite_id, "valid": False, "status": "error"}







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
