"""
Query functions for the community module -- kept separate from the admin
dashboard's crud.py so the two stay easy to reason about independently.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from community.models import Account, Friendship

# A member counts as "online" if we've seen a request from them in the
# last 3 minutes. Cheap to compute, no background job or websocket needed.
ONLINE_WINDOW = timedelta(minutes=3)


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
) -> Account | None:
    account = await get_account_by_id(db, account_id)
    if not account:
        return None

    account.is_verified = is_verified
    account.role_label = role_label or None
    account.role_color_start = role_color_start or None
    account.role_color_end = role_color_end or None
    account.is_banned = is_banned

    await db.commit()
    await db.refresh(account)
    return account


async def delete_account(db: AsyncSession, account_id: int) -> bool:
    account = await get_account_by_id(db, account_id)
    if not account:
        return False
    await db.delete(account)
    await db.commit()
    return True
