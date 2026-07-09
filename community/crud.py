"""
Query functions for the community module -- kept separate from the admin
dashboard's crud.py so the two stay easy to reason about independently.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from community.models import Account, Channel, Comment, Friendship, Gift, GiftInstance, Post, PostLike

# A member counts as "online" if we've seen a request from them in the
# last 3 minutes. Cheap to compute, no background job or websocket needed.
ONLINE_WINDOW = timedelta(minutes=3)

VISUAL_NAME_EFFECTS = {"none", "gradient", "glow"}
VISUAL_NAME_FONTS = {"default", "mono", "serif", "rounded", "cyber", "display"}


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
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_effect VARCHAR(32)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_color_start VARCHAR(16)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_color_end VARCHAR(16)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS name_font VARCHAR(32)"))
    await db.execute(text("ALTER TABLE community_accounts ADD COLUMN IF NOT EXISTS profile_card_bg_url VARCHAR(512)"))
    await db.commit()



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
        await db.commit()


async def list_online_accounts(db: AsyncSession, limit: int = 50) -> list[Account]:
    since = datetime.now(timezone.utc) - ONLINE_WINDOW
    result = await db.execute(
        select(Account)
        .where(Account.last_seen_at >= since, Account.is_banned == False)  # noqa: E712
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

    # Remove issued gifts first so the new gift foreign key never blocks
    # deleting a community account from the admin panel.
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

