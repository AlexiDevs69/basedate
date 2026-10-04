"""
Auth helpers for the community module: password hashing, Telegram Login
Widget signature verification, session helpers, a small in-memory rate
limiter and a same-origin (CSRF / WebSocket hijacking) check.

Uses the SAME signed session cookie as the admin dashboard (one
SessionMiddleware for the whole app), but under a different key
(SESSION_KEY below), so a visitor's community login and the admin's own
login can never collide or leak into each other.
"""
import hashlib
import hmac
import os
import time
from collections import deque
from urllib.parse import urlsplit

import bcrypt
from fastapi import Request

from config import get_settings

settings = get_settings()

SESSION_KEY = "community_account_id"
SESSION_VERSION_KEY = "community_session_version"

# bcrypt only looks at the first 72 bytes of the password.
MAX_PASSWORD_BYTES = 72


# --- Passwords ---------------------------------------------------------

def hash_password(raw_password: str) -> str:
    raw = raw_password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        # Never silently truncate: newer bcrypt versions raise, older ones
        # truncate, which would make two different passwords equivalent.
        raise ValueError("password too long")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(raw_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(raw_password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash (e.g. an account with no real password set) --
        # never let a broken hash accidentally verify as a match.
        return False


# A valid bcrypt hash of a random value. Login verifies against it when the
# account does not exist, so "unknown email" and "wrong password" take the
# same time and cannot be told apart by timing (user enumeration).
DUMMY_PASSWORD_HASH = bcrypt.hashpw(os.urandom(16), bcrypt.gensalt()).decode("utf-8")


# --- Rate limiting ------------------------------------------------------
# In-memory sliding window. Good enough for a single-process deployment
# (Render free tier); with several workers each one keeps its own counters,
# which only makes the limit looser, never stricter.

class SlidingWindowLimiter:
    def __init__(self, max_keys: int = 20000) -> None:
        self._hits: dict[str, deque] = {}
        self._max_keys = max_keys

    def _prune(self, key: str, window: float, now: float) -> deque:
        bucket = self._hits.get(key)
        if bucket is None:
            return deque()
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if not bucket:
            self._hits.pop(key, None)
            return deque()
        return bucket

    def blocked(self, key: str, limit: int, window: float) -> bool:
        return len(self._prune(key, window, time.monotonic())) >= limit

    def record(self, key: str, window: float) -> None:
        now = time.monotonic()
        bucket = self._prune(key, window, now)
        bucket.append(now)
        self._hits[key] = bucket
        if len(self._hits) > self._max_keys:
            # Memory guard: drop every bucket whose newest hit is stale.
            for stale in [k for k, b in self._hits.items() if not b or now - b[-1] > window]:
                self._hits.pop(stale, None)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


rate_limiter = SlidingWindowLimiter()


def client_ip(request: Request) -> str:
    client = request.client
    return client.host if client and client.host else "unknown"


# --- Same-origin check (CSRF + cross-site WebSocket hijacking) ------------

def _extra_allowed_hosts() -> set[str]:
    raw = os.getenv("COMMUNITY_ALLOWED_ORIGINS", "")
    hosts: set[str] = set()
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        hosts.add(urlsplit(item).netloc.lower() if "://" in item else item)
    return hosts


def origin_is_trusted(conn) -> bool:
    """
    True when the request carries no Origin header (non-browser client) or
    when its Origin host matches the host the request was sent to.

    Browsers always send Origin on cross-site POSTs and on every WebSocket
    handshake, so a malicious page cannot forge a request with the victim's
    cookie. Extra hosts can be allowed via COMMUNITY_ALLOWED_ORIGINS
    (comma separated, e.g. "example.com,www.example.com").
    """
    origin = (conn.headers.get("origin") or "").strip()
    if not origin or origin.lower() == "null":
        # "null" is sent by sandboxed iframes / file:// pages -- not ours.
        return not origin
    origin_host = urlsplit(origin).netloc.lower()
    if not origin_host:
        return False

    allowed = _extra_allowed_hosts()
    host = (conn.headers.get("host") or "").strip().lower()
    if host:
        allowed.add(host)
    forwarded_host = (conn.headers.get("x-forwarded-host") or "").split(",")[0].strip().lower()
    if forwarded_host:
        allowed.add(forwarded_host)
    return origin_host in allowed


# --- Telegram Login Widget -----------------------------------------------
# Verification algorithm per https://core.telegram.org/widgets/login

def verify_telegram_login(data: dict) -> bool:
    """
    `data` is every query param Telegram sent back (id, first_name,
    username, photo_url, auth_date, hash, ...). Returns True only if the
    signature is valid AND the login happened in the last 24h (blocks
    someone replaying an old, captured login URL).
    """
    received_hash = data.get("hash")
    if not received_hash or not settings.bot_token:
        return False

    check_fields = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(check_fields.items()))

    secret_key = hashlib.sha256(settings.bot_token.encode("utf-8")).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, str(received_hash)):
        return False

    try:
        auth_date = int(data.get("auth_date", 0))
        int(data.get("id", ""))
    except (TypeError, ValueError):
        return False

    age = time.time() - auth_date
    # Reject stale logins AND logins dated in the future (clock tricks).
    return -60 <= age <= 86400


# --- Session helpers -------------------------------------------------------

def log_in(request: Request, account_id: int, session_version: int = 1) -> None:
    request.session[SESSION_KEY] = account_id
    request.session[SESSION_VERSION_KEY] = max(1, int(session_version or 1))


def log_out(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)
    request.session.pop(SESSION_VERSION_KEY, None)


def get_logged_in_account_id(request: Request) -> int | None:
    return request.session.get(SESSION_KEY)


def get_logged_in_session_version(request: Request) -> int:
    try:
        return max(1, int(request.session.get(SESSION_VERSION_KEY) or 1))
    except (TypeError, ValueError):
        return 1
