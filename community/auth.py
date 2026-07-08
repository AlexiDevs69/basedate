"""
Auth helpers for the community module: password hashing, Telegram Login
Widget signature verification, and session helpers.

Uses the SAME signed session cookie as the admin dashboard (one
SessionMiddleware for the whole app), but under a different key
(SESSION_KEY below), so a visitor's community login and the admin's own
login can never collide or leak into each other.
"""
import hashlib
import hmac
import time

import bcrypt
from fastapi import Request

from config import get_settings

settings = get_settings()

SESSION_KEY = "community_account_id"


# --- Passwords ---------------------------------------------------------

def hash_password(raw_password: str) -> str:
    return bcrypt.hashpw(raw_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(raw_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash (e.g. an account with no real password set) --
        # never let a broken hash accidentally verify as a match.
        return False


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

    if not hmac.compare_digest(computed_hash, received_hash):
        return False

    try:
        auth_date = int(data.get("auth_date", 0))
    except ValueError:
        return False

    return (time.time() - auth_date) <= 86400


# --- Session helpers -------------------------------------------------------

def log_in(request: Request, account_id: int) -> None:
    request.session[SESSION_KEY] = account_id


def log_out(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


def get_logged_in_account_id(request: Request) -> int | None:
    return request.session.get(SESSION_KEY)

