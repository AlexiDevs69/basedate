"""
Routes for the public-facing community module: registration, login
(email/password + Telegram Login Widget), logout, the main "who's online"
page, public profile pages, and self-service profile editing.

Mounted into the main app via `app.include_router(community_router)` in
main.py -- everything here lives under the /community prefix so it can
never collide with the admin dashboard's routes.
"""
import asyncio
import json
import hashlib
import math
import os
import re
import time
import uuid
from pathlib import Path

import httpx

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from community import auth, crud
from config import get_settings
from database import AsyncSessionLocal, get_db

settings = get_settings()
router = APIRouter(prefix="/community", tags=["community"])

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
SUPPORTED_LANGUAGES = {"ru", "uk", "en"}
DEFAULT_LANGUAGE = "ru"
_LANGUAGE_META = {
    "ru": {"code": "ru", "flag": "🇷🇺", "name": "Русский", "native": "Русский"},
    "uk": {"code": "uk", "flag": "🇺🇦", "name": "Українська", "native": "Українська"},
    "en": {"code": "en", "flag": "🇺🇸", "name": "English", "native": "English"},
}
_LOCALE_CACHE: dict[str, dict] = {}


def _normalize_language(value: str | None) -> str:
    lang = (value or DEFAULT_LANGUAGE).strip().lower()
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def _load_locale(language: str | None) -> dict:
    lang = _normalize_language(language)
    if lang in _LOCALE_CACHE:
        return _LOCALE_CACHE[lang]
    path = LOCALES_DIR / f"{lang}.json"
    fallback_path = LOCALES_DIR / f"{DEFAULT_LANGUAGE}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            data = json.loads(fallback_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    _LOCALE_CACHE[lang] = data
    return data


def _language_response_payload(language: str | None) -> dict:
    lang = _normalize_language(language)
    return {
        "ok": True,
        "language": lang,
        "languages": list(_LANGUAGE_META.values()),
        "messages": _load_locale(lang),
    }

ROOT_DIR = Path(__file__).resolve().parents[1]
PROFILE_UPLOAD_DIR = ROOT_DIR / "static" / "uploads" / "profiles"
ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
MAX_PROFILE_IMAGE_BYTES = 5 * 1024 * 1024


def _safe_next_url(next_url: str | None, fallback: str = "/community") -> str:
    target = (next_url or "").strip()
    if target.startswith("/community") and not target.startswith("//"):
        return target
    return fallback


def _forbidden_response() -> PlainTextResponse:
    return PlainTextResponse("Forbidden", status_code=403)


SERVER_BANNER_FALLBACK = "linear-gradient(135deg,#111,#555)"
ALLOWED_SERVER_BANNERS = {
    "linear-gradient(135deg,#111,#555)",
    "linear-gradient(135deg,#ff2e9f,#ff6ad5)",
    "linear-gradient(135deg,#ff2222,#ff6b5f)",
    "linear-gradient(135deg,#ff7a18,#ffbd4a)",
    "linear-gradient(135deg,#ffe259,#ffa751)",
    "linear-gradient(135deg,#7f35bd,#c471ed)",
    "linear-gradient(135deg,#20c6ff,#4facfe)",
    "linear-gradient(135deg,#43e97b,#38f9d7)",
    "linear-gradient(135deg,#3a7d0f,#7ed957)",
    "linear-gradient(135deg,#222,#aaa)",
}


def _clean_server_banner(value: str | None) -> str:
    clean = (value or "").strip()
    return clean if clean in ALLOWED_SERVER_BANNERS else SERVER_BANNER_FALLBACK


async def _ensure_server_visual_columns(db: AsyncSession) -> None:
    # Safe Render migration: create_all does not add columns to old tables.
    await db.execute(text("ALTER TABLE community_servers ADD COLUMN IF NOT EXISTS banner_color VARCHAR(255)"))
    await db.execute(text("ALTER TABLE community_server_messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP WITH TIME ZONE"))
    await db.execute(text("ALTER TABLE community_direct_messages ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP WITH TIME ZONE"))
    await db.execute(text("ALTER TABLE community_server_messages ADD COLUMN IF NOT EXISTS reply_to_id INTEGER"))
    await db.execute(text("ALTER TABLE community_direct_messages ADD COLUMN IF NOT EXISTS reply_to_id INTEGER"))
    await db.execute(text("ALTER TABLE community_server_messages ADD COLUMN IF NOT EXISTS is_forwarded BOOLEAN NOT NULL DEFAULT FALSE"))
    await db.execute(text("ALTER TABLE community_direct_messages ADD COLUMN IF NOT EXISTS is_forwarded BOOLEAN NOT NULL DEFAULT FALSE"))
    await db.commit()


async def _get_server_banner_color(db: AsyncSession, server_id: int) -> str:
    await _ensure_server_visual_columns(db)
    result = await db.execute(
        text("SELECT banner_color FROM community_servers WHERE id = :server_id"),
        {"server_id": server_id},
    )
    return _clean_server_banner(result.scalar_one_or_none())


async def _set_server_banner_color(db: AsyncSession, server_id: int, banner_color: str) -> None:
    await _ensure_server_visual_columns(db)
    await db.execute(
        text("UPDATE community_servers SET banner_color = :banner_color WHERE id = :server_id"),
        {"server_id": server_id, "banner_color": _clean_server_banner(banner_color)},
    )
    await db.commit()


async def _read_profile_upload(upload: UploadFile | None) -> tuple[bytes, str] | None:
    if upload is None or not getattr(upload, "filename", None):
        return None
    content_type = (upload.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        return None
    data = await upload.read()
    if not data or len(data) > MAX_PROFILE_IMAGE_BYTES:
        return None
    return data, content_type


async def _upload_to_cloudinary(data: bytes, content_type: str) -> str | None:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()
    folder = os.getenv("CLOUDINARY_FOLDER", "alexihub/profiles").strip() or "alexihub/profiles"
    if not (cloud_name and api_key and api_secret):
        return None

    # Signed Cloudinary upload without adding a new Python dependency.
    # Only the final URL is stored in PostgreSQL; the image bytes never go into the DB.
    timestamp = str(int(time.time()))
    params_to_sign = {"folder": folder, "timestamp": timestamp}
    signature_base = "&".join(f"{k}={v}" for k, v in sorted(params_to_sign.items())) + api_secret
    signature = hashlib.sha1(signature_base.encode("utf-8")).hexdigest()

    ext = ALLOWED_IMAGE_TYPES.get(content_type, ".png")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
                data={
                    "api_key": api_key,
                    "timestamp": timestamp,
                    "folder": folder,
                    "signature": signature,
                },
                files={"file": (f"profile{ext}", data, content_type)},
            )
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code < 300 and payload.get("secure_url"):
            return payload["secure_url"]
        if resp.status_code < 300 and payload.get("url"):
            return payload["url"]
        print("Cloudinary upload failed:", resp.status_code, payload)
    except Exception as exc:
        print("Cloudinary upload error:", repr(exc))
    return None


async def _upload_to_imgur(data: bytes, content_type: str) -> str | None:
    # Legacy fallback. Cloudinary is preferred.
    client_id = os.getenv("IMGUR_CLIENT_ID", "").strip()
    if not client_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.imgur.com/3/image",
                headers={"Authorization": f"Client-ID {client_id}"},
                files={"image": ("profile" + ALLOWED_IMAGE_TYPES.get(content_type, ".png"), data, content_type)},
            )
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code < 300 and payload.get("success") and payload.get("data", {}).get("link"):
            return payload["data"]["link"]
    except Exception:
        return None
    return None


def _save_profile_upload_local(data: bytes, content_type: str, account_id: int, kind: str) -> str:
    PROFILE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = ALLOWED_IMAGE_TYPES.get(content_type, ".png")
    filename = f"{account_id}_{kind}_{uuid.uuid4().hex[:18]}{ext}"
    path = PROFILE_UPLOAD_DIR / filename
    path.write_bytes(data)
    return f"/static/uploads/profiles/{filename}"


async def _profile_image_url_from_form(upload: UploadFile | None, url_value: str, account_id: int, kind: str) -> str:
    prepared = await _read_profile_upload(upload)
    if prepared:
        data, content_type = prepared
        external_url = await _upload_to_cloudinary(data, content_type)
        if not external_url:
            external_url = await _upload_to_imgur(data, content_type)
        if external_url:
            return external_url
        return _save_profile_upload_local(data, content_type, account_id, kind)
    return (url_value or "").strip()


@router.on_event("startup")
async def community_schema_startup() -> None:
    # create_all() does not add new columns to existing tables; this makes
    # visual/profile/presence columns safe on Render without Alembic.
    async with AsyncSessionLocal() as db:
        await crud.ensure_account_visual_columns(db)
        await _ensure_server_visual_columns(db)
        await crud.ensure_reaction_tables(db)
        await crud.ensure_mention_table(db)


# --- Lightweight realtime layer --------------------------------------------
# This is intentionally in-memory: typing state is NOT written to PostgreSQL.
# DB is touched only when a real message is created/edited/deleted.
class RealtimeChannelManager:
    def __init__(self) -> None:
        self.connections: dict[tuple[int, int], dict[int, set[WebSocket]]] = {}
        self.typing: dict[tuple[int, int], dict[int, dict]] = {}
        self.lock = asyncio.Lock()

    def _account_connection_count_unlocked(self, account_id: int) -> int:
        total = 0
        for users in self.connections.values():
            total += len(users.get(account_id, set()))
        return total

    async def connect(self, key: tuple[int, int], account_id: int, websocket: WebSocket, profile: dict | None = None) -> None:
        await websocket.accept()
        async with self.lock:
            self.connections.setdefault(key, {}).setdefault(account_id, set()).add(websocket)

    async def disconnect(self, key: tuple[int, int], account_id: int, websocket: WebSocket, profile: dict | None = None) -> None:
        async with self.lock:
            users = self.connections.get(key)
            if users and account_id in users:
                users[account_id].discard(websocket)
                if not users[account_id]:
                    users.pop(account_id, None)
            if users == {}:
                self.connections.pop(key, None)
            if key in self.typing:
                self.typing[key].pop(account_id, None)
                if not self.typing[key]:
                    self.typing.pop(key, None)
        await self.broadcast_typing(key)

    async def is_account_connected(self, key: tuple[int, int], account_id: int) -> bool:
        async with self.lock:
            return bool(self.connections.get(key, {}).get(int(account_id)))

    async def broadcast_presence_for_scope(self, key: tuple[int, int], payload: dict) -> None:
        # Server presence must update every open channel of the same server,
        # not only the current channel. DM presence stays limited to the DM thread.
        if key[0] == 0:
            await self.broadcast(key, payload)
            return
        async with self.lock:
            keys = [k for k in self.connections.keys() if k[0] == key[0]]
        for k in keys:
            await self.broadcast(k, payload)

    async def broadcast_presence_everywhere(self, payload: dict) -> None:
        async with self.lock:
            keys = list(self.connections.keys())
        for k in keys:
            await self.broadcast(k, payload)

    async def broadcast(self, key: tuple[int, int], payload: dict) -> None:
        async with self.lock:
            sockets = [ws for by_user in self.connections.get(key, {}).values() for ws in by_user]
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self.lock:
                users = self.connections.get(key, {})
                for account_id, account_sockets in list(users.items()):
                    for ws in dead:
                        account_sockets.discard(ws)
                    if not account_sockets:
                        users.pop(account_id, None)
                        if key in self.typing:
                            self.typing[key].pop(account_id, None)
                if not users:
                    self.connections.pop(key, None)
                if key in self.typing and not self.typing[key]:
                    self.typing.pop(key, None)

    async def set_typing(self, key: tuple[int, int], account_id: int, profile: dict) -> None:
        expires_at = time.monotonic() + 3.0
        async with self.lock:
            self.typing.setdefault(key, {})[account_id] = {**profile, "expires_at": expires_at}
        await self.broadcast_typing(key)

    async def clear_typing(self, key: tuple[int, int], account_id: int) -> None:
        async with self.lock:
            if key in self.typing:
                self.typing[key].pop(account_id, None)
                if not self.typing[key]:
                    self.typing.pop(key, None)
        await self.broadcast_typing(key)

    async def broadcast_typing(self, key: tuple[int, int]) -> None:
        now = time.monotonic()
        async with self.lock:
            typers_map = self.typing.get(key, {})
            expired = [uid for uid, item in typers_map.items() if item.get("expires_at", 0) <= now]
            for uid in expired:
                typers_map.pop(uid, None)
            if not typers_map and key in self.typing:
                self.typing.pop(key, None)
            users = [
                {"id": uid, "username": item.get("username", "user")}
                for uid, item in typers_map.items()
            ]
        await self.broadcast(key, {"type": "typing", "users": users})


realtime_channels = RealtimeChannelManager()


class MessageRateLimiter:
    """Small server-side anti-spam guard shared by DM and server messages.

    The limiter is deliberately global per account, so switching channels or
    using the HTTP fallback cannot bypass it.  State is kept in memory because
    this app currently runs a single realtime process; a multi-worker deploy
    should move the same counters to Redis.
    """

    BURST_SIZE = 5
    BURST_WINDOW_SECONDS = 5.0
    VIOLATION_WINDOW_SECONDS = 10 * 60.0
    COOLDOWN_SECONDS = (5, 30, 120)
    IDLE_TTL_SECONDS = 60 * 60.0

    def __init__(self) -> None:
        self.activity: dict[int, list[float]] = {}
        self.violations: dict[int, list[float]] = {}
        self.blocked_until: dict[int, float] = {}
        self.last_seen: dict[int, float] = {}
        self.lock = asyncio.Lock()
        self._last_cleanup = time.monotonic()

    def _cleanup_unlocked(self, now: float) -> None:
        if now - self._last_cleanup < 300.0:
            return
        cutoff = now - self.IDLE_TTL_SECONDS
        stale = [account_id for account_id, seen_at in self.last_seen.items() if seen_at < cutoff]
        for account_id in stale:
            self.activity.pop(account_id, None)
            self.violations.pop(account_id, None)
            self.blocked_until.pop(account_id, None)
            self.last_seen.pop(account_id, None)
        self._last_cleanup = now

    async def check(self, account_id: int) -> int:
        """Record one attempted message and return cooldown milliseconds.

        A zero return value means the message is allowed.  Repeated bursts in
        ten minutes escalate from 5 seconds to 30 seconds and then 2 minutes.
        Attempts during an active cooldown do not extend it.
        """
        account_id = int(account_id)
        now = time.monotonic()
        async with self.lock:
            self._cleanup_unlocked(now)
            self.last_seen[account_id] = now

            blocked_until = self.blocked_until.get(account_id, 0.0)
            if blocked_until > now:
                return max(1, math.ceil((blocked_until - now) * 1000.0))
            self.blocked_until.pop(account_id, None)

            activity_cutoff = now - self.BURST_WINDOW_SECONDS
            recent = [stamp for stamp in self.activity.get(account_id, []) if stamp > activity_cutoff]
            if len(recent) >= self.BURST_SIZE:
                violation_cutoff = now - self.VIOLATION_WINDOW_SECONDS
                violations = [
                    stamp for stamp in self.violations.get(account_id, []) if stamp > violation_cutoff
                ]
                cooldown = self.COOLDOWN_SECONDS[min(len(violations), len(self.COOLDOWN_SECONDS) - 1)]
                violations.append(now)
                self.violations[account_id] = violations
                self.activity[account_id] = []
                self.blocked_until[account_id] = now + cooldown
                return cooldown * 1000

            recent.append(now)
            self.activity[account_id] = recent
            return 0


message_rate_limiter = MessageRateLimiter()


def _message_rate_limit_payload(retry_after_ms: int) -> dict:
    retry_after_ms = max(1, int(retry_after_ms))
    return {
        "type": "rate_limited",
        "error": "message_rate_limited",
        "retry_after_ms": retry_after_ms,
        "retry_after_seconds": max(1, math.ceil(retry_after_ms / 1000.0)),
        "message": "Ви надсилаєте повідомлення надто швидко.",
    }


def _message_rate_limit_redirect(url: str, retry_after_ms: int) -> RedirectResponse:
    seconds = max(1, math.ceil(int(retry_after_ms) / 1000.0))
    separator = "&" if "?" in url else "?"
    return RedirectResponse(
        url=f"{url}{separator}rate_limited={seconds}",
        status_code=303,
        headers={"Retry-After": str(seconds)},
    )


def _message_rate_limit_json_response(
    retry_after_ms: int, *, sent: list | None = None
) -> JSONResponse:
    payload = _message_rate_limit_payload(retry_after_ms)
    payload["ok"] = False
    if sent is not None:
        payload["sent"] = sent
        payload["count"] = len(sent)
    return JSONResponse(
        payload,
        status_code=429,
        headers={"Retry-After": str(payload["retry_after_seconds"])},
    )


class AccountRealtimeManager:
    """One lightweight WebSocket per open AlexiHub page.

    It powers global account presence and DM sidebar ordering. A short offline
    grace period prevents the user from blinking offline while navigating
    between pages.
    """

    OFFLINE_GRACE_SECONDS = 12.0

    def __init__(self) -> None:
        self.connections: dict[int, set[WebSocket]] = {}
        self.profiles: dict[int, dict] = {}
        self.offline_tasks: dict[int, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    @staticmethod
    def _public_presence_payload(profile: dict | None, connected: bool) -> dict:
        profile = profile or {}
        account_id = int(profile.get("id") or 0)
        raw_status = str(profile.get("account_status") or "online").strip().lower()
        visible = connected and raw_status != "invisible"
        return {
            "type": "presence",
            "account_id": account_id,
            "username": profile.get("username") or "",
            "online": visible,
            "status": raw_status if visible else "offline",
        }

    async def connect(self, account_id: int, websocket: WebSocket, profile: dict) -> None:
        await websocket.accept()
        pending_task = None
        async with self.lock:
            pending_task = self.offline_tasks.pop(account_id, None)
            was_disconnected = not self.connections.get(account_id)
            self.connections.setdefault(account_id, set()).add(websocket)
            self.profiles[account_id] = dict(profile)
            snapshot = [
                self._public_presence_payload(self.profiles.get(uid), bool(sockets))
                for uid, sockets in self.connections.items()
                if sockets
            ]
        if pending_task:
            pending_task.cancel()
        try:
            await websocket.send_json({"type": "presence_snapshot", "accounts": snapshot})
        except Exception:
            pass
        if was_disconnected:
            await self.broadcast_all(self._public_presence_payload(profile, True))

    async def disconnect(self, account_id: int, websocket: WebSocket) -> None:
        async with self.lock:
            sockets = self.connections.get(account_id)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    self.connections.pop(account_id, None)
            if self.connections.get(account_id):
                return
            old_task = self.offline_tasks.pop(account_id, None)
            if old_task:
                old_task.cancel()
            task = asyncio.create_task(self._broadcast_offline_after_grace(account_id))
            self.offline_tasks[account_id] = task

    async def _broadcast_offline_after_grace(self, account_id: int) -> None:
        try:
            await asyncio.sleep(self.OFFLINE_GRACE_SECONDS)
            async with self.lock:
                if self.connections.get(account_id):
                    return
                self.offline_tasks.pop(account_id, None)
                profile = dict(self.profiles.get(account_id) or {"id": account_id})
            await self.broadcast_all(self._public_presence_payload(profile, False))
        except asyncio.CancelledError:
            return

    async def broadcast_all(self, payload: dict) -> None:
        async with self.lock:
            sockets = [ws for group in self.connections.values() for ws in group]
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self.lock:
                for account_id, group in list(self.connections.items()):
                    for ws in dead:
                        group.discard(ws)
                    if not group:
                        self.connections.pop(account_id, None)

    async def send_to_account(self, account_id: int, payload: dict) -> None:
        async with self.lock:
            sockets = list(self.connections.get(int(account_id), set()))
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self.lock:
                group = self.connections.get(int(account_id), set())
                for ws in dead:
                    group.discard(ws)
                if not group:
                    self.connections.pop(int(account_id), None)

    async def set_profile_and_broadcast(self, profile: dict) -> None:
        account_id = int(profile.get("id") or 0)
        if not account_id:
            return
        async with self.lock:
            self.profiles[account_id] = dict(profile)
            connected = bool(self.connections.get(account_id))
        await self.broadcast_all(self._public_presence_payload(profile, connected))

    async def presence_snapshot_for(self, account_ids: list[int]) -> list[dict]:
        unique_ids = []
        seen = set()
        for raw_id in account_ids[:500]:
            try:
                account_id = int(raw_id)
            except Exception:
                continue
            if account_id <= 0 or account_id in seen:
                continue
            seen.add(account_id)
            unique_ids.append(account_id)
        async with self.lock:
            return [
                self._public_presence_payload(
                    self.profiles.get(account_id) or {"id": account_id},
                    bool(self.connections.get(account_id)),
                )
                for account_id in unique_ids
            ]

    async def public_status(self, account_id: int, fallback_profile: dict | None = None) -> tuple[bool, str]:
        async with self.lock:
            connected = bool(self.connections.get(int(account_id)))
            profile = dict(self.profiles.get(int(account_id)) or fallback_profile or {"id": account_id})
        payload = self._public_presence_payload(profile, connected)
        return bool(payload["online"]), str(payload["status"])


account_realtime = AccountRealtimeManager()


async def _mention_counts_for(account_id: int) -> dict:
    async with AsyncSessionLocal() as db:
        return await crud.unread_mention_summary(db, int(account_id))


async def _broadcast_mention_counts(account_ids) -> None:
    unique_ids = sorted({int(uid) for uid in (account_ids or []) if int(uid or 0) > 0})
    for account_id in unique_ids:
        try:
            counts = await _mention_counts_for(account_id)
            await account_realtime.send_to_account(
                account_id,
                {"type": "mention_counts", "counts": counts},
            )
        except Exception as exc:
            print("Mention count realtime update failed:", repr(exc))


async def _sync_dm_message_mentions(db: AsyncSession, message) -> list[int]:
    mentioned, affected = await crud.sync_dm_mentions(db, message)
    key = (0, int(message.thread_id))
    for target_id in mentioned:
        if await realtime_channels.is_account_connected(key, target_id):
            await crud.mark_dm_mentions_read(db, target_id, int(message.thread_id))
    return affected


async def _sync_server_message_mentions(db: AsyncSession, message) -> list[int]:
    mentioned, affected = await crud.sync_server_mentions(db, message)
    key = (int(message.server_id), int(message.channel_id))
    for target_id in mentioned:
        if await realtime_channels.is_account_connected(key, target_id):
            await crud.mark_server_channel_mentions_read(
                db, target_id, int(message.server_id), int(message.channel_id)
            )
    return affected


async def _emit_dm_sidebar_update(thread_id: int, message_id: int) -> None:
    """Move the relevant DM row to the top for both participants in realtime."""
    try:
        async with AsyncSessionLocal() as db:
            thread = await crud.get_dm_thread_by_id(db, int(thread_id))
            message = await crud.get_dm_message(db, int(thread_id), int(message_id))
            if not thread or not message:
                return
            sender = await crud.get_account_by_id(db, int(message.author_id))
            if not sender:
                return
            participant_ids = [int(thread.user_low_id), int(thread.user_high_id)]
            if int(sender.id) not in participant_ids:
                return
            recipient_id = participant_ids[1] if participant_ids[0] == int(sender.id) else participant_ids[0]
            recipient = await crud.get_account_by_id(db, recipient_id)
            if not recipient:
                return

            last_message = {
                "id": int(message.id),
                "author_id": int(message.author_id),
                "content": (message.content or "")[:4000],
                "image_url": message.image_url or None,
                "created_at": message.created_at.isoformat(),
                "is_forwarded": bool(getattr(message, "is_forwarded", False)),
            }
            updated_at = (
                thread.updated_at.isoformat()
                if getattr(thread, "updated_at", None)
                else message.created_at.isoformat()
            )

            for viewer_id, other in (
                (int(sender.id), recipient),
                (int(recipient.id), sender),
            ):
                other_profile = _account_payload(other)
                other_online, other_status = await account_realtime.public_status(int(other.id), other_profile)
                await account_realtime.send_to_account(
                    viewer_id,
                    {
                        "type": "dm_sidebar_update",
                        "thread_id": int(thread.id),
                        "updated_at": updated_at,
                        "author_id": int(message.author_id),
                        "last_message": last_message,
                        "other": other_profile,
                        "other_online": other_online,
                        "other_status": other_status,
                    },
                )
    except Exception as exc:
        print("DM sidebar realtime update failed:", repr(exc))


def _ws_account_id(websocket: WebSocket) -> int | None:
    try:
        account_id = auth.get_logged_in_account_id(websocket)  # works because WebSocket has .session too
        return int(account_id) if account_id else None
    except Exception:
        session = getattr(websocket, "session", {}) or {}
        for key in ("community_account_id", "account_id", "community_user_id"):
            value = session.get(key)
            if value:
                return int(value)
        return None


def _account_payload(account) -> dict:
    return {
        "id": account.id,
        "username": account.username,
        "avatar_url": account.avatar_url,
        "banner_url": account.banner_url,
        "role_label": account.role_label,
        "role_color_start": account.role_color_start or "#f5576c",
        "role_color_end": account.role_color_end or "#7367f0",
        "name_effect": account.name_effect or "none",
        "name_font": account.name_font or "default",
        "account_status": account.account_status or "online",
        "bio": account.bio or "",
        "is_verified": bool(getattr(account, "is_verified", False)),
        "language": getattr(account, "language", DEFAULT_LANGUAGE) or DEFAULT_LANGUAGE,
    }


def _parse_optional_int(value) -> int | None:
    try:
        clean = str(value or "").strip()
        return int(clean) if clean else None
    except Exception:
        return None


def _reply_payload(message, author) -> dict | None:
    if not message:
        return None
    return {
        "id": message.id,
        "content": message.content or "",
        "image_url": message.image_url,
        "author": _account_payload(author) if author else {"id": None, "username": "видалений юзер", "avatar_url": ""},
    }


def _missing_author_payload(author_id: int | None) -> dict:
    return {
        "id": int(author_id or 0),
        "username": "видалений юзер",
        "avatar_url": "",
        "banner_url": "",
        "role_label": "member",
        "role_color_start": "#f5576c",
        "role_color_end": "#7367f0",
        "name_effect": "none",
        "name_font": "default",
        "account_status": "offline",
        "bio": "",
        "is_verified": False,
        "language": DEFAULT_LANGUAGE,
    }


async def _server_message_realtime_event(
    db: AsyncSession,
    message,
    *,
    client_nonce: str | None = None,
) -> dict:
    author = await crud.get_account_by_id(db, int(message.author_id))
    reply = None
    reply_id = getattr(message, "reply_to_id", None)
    if reply_id:
        reply_message = await crud.get_server_message(
            db, int(message.server_id), int(message.channel_id), int(reply_id)
        )
        if reply_message:
            reply_author = await crud.get_account_by_id(db, int(reply_message.author_id))
            reply = _reply_payload(reply_message, reply_author)
    payload = {
        "type": "message",
        "message": {
            "id": int(message.id),
            "server_id": int(message.server_id),
            "channel_id": int(message.channel_id),
            "author_id": int(message.author_id),
            "content": message.content or "",
            "image_url": message.image_url or None,
            "created_at": message.created_at.isoformat(),
            "edited_at": (
                message.edited_at.isoformat()
                if getattr(message, "edited_at", None)
                else None
            ),
            "reply_to_id": int(reply_id) if reply_id else None,
            "reply": reply,
            "is_forwarded": bool(getattr(message, "is_forwarded", False)),
        },
        "author": _account_payload(author) if author else _missing_author_payload(message.author_id),
    }
    if client_nonce:
        payload["client_nonce"] = client_nonce
    return payload


async def _dm_message_realtime_event(
    db: AsyncSession,
    message,
    *,
    client_nonce: str | None = None,
) -> dict:
    author = await crud.get_account_by_id(db, int(message.author_id))
    reply = None
    reply_id = getattr(message, "reply_to_id", None)
    if reply_id:
        reply_message = await crud.get_dm_message(db, int(message.thread_id), int(reply_id))
        if reply_message:
            reply_author = await crud.get_account_by_id(db, int(reply_message.author_id))
            reply = _reply_payload(reply_message, reply_author)
    payload = {
        "type": "message",
        "message": {
            "id": int(message.id),
            "thread_id": int(message.thread_id),
            "author_id": int(message.author_id),
            "content": message.content or "",
            "image_url": message.image_url or None,
            "created_at": message.created_at.isoformat(),
            "edited_at": (
                message.edited_at.isoformat()
                if getattr(message, "edited_at", None)
                else None
            ),
            "reply_to_id": int(reply_id) if reply_id else None,
            "reply": reply,
            "is_forwarded": bool(getattr(message, "is_forwarded", False)),
        },
        "author": _account_payload(author) if author else _missing_author_payload(message.author_id),
    }
    if client_nonce:
        payload["client_nonce"] = client_nonce
    return payload


@router.websocket("/ws/account")
async def ws_account_realtime(websocket: WebSocket):
    """Global realtime channel for DM sidebar ordering and presence."""
    account_id = _ws_account_id(websocket)
    if not account_id:
        await websocket.close(code=1008)
        return

    async with AsyncSessionLocal() as db:
        await crud.touch_last_seen(db, int(account_id))
        account = await crud.get_account_by_id(db, int(account_id))
        if not account or account.is_banned:
            await websocket.close(code=1008)
            return
        profile = _account_payload(account)

    await account_realtime.connect(int(account_id), websocket, profile)
    try:
        await websocket.send_json({
            "type": "mention_snapshot",
            "counts": await _mention_counts_for(int(account_id)),
        })
    except Exception:
        pass
    try:
        while True:
            data = await websocket.receive_json()
            event_type = str(data.get("type") or "").strip().lower()
            if event_type in {"leave", "disconnect", "close"}:
                break
            if event_type == "watch_presence":
                raw_ids = data.get("account_ids") or []
                if not isinstance(raw_ids, list):
                    raw_ids = []
                accounts = await account_realtime.presence_snapshot_for(raw_ids)
                await websocket.send_json({"type": "presence_snapshot", "accounts": accounts})
                continue
            if event_type == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await account_realtime.disconnect(int(account_id), websocket)



async def current_account(request: Request, db: AsyncSession):
    """Returns the logged-in Account for this visitor, or None."""
    account_id = auth.get_logged_in_account_id(request)
    if not account_id:
        return None
    return await crud.get_account_by_id(db, account_id)


@router.get("/api/i18n")
async def api_i18n(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    requested_language_raw = request.query_params.get("language") or request.query_params.get("lang")
    requested_language = _normalize_language(requested_language_raw) if requested_language_raw else None
    cookie_language = _normalize_language(request.cookies.get("alexihub_language")) if request.cookies.get("alexihub_language") else None

    # Якщо frontend просить конкретну мову для миттєвого свапу, віддаємо саме її.
    # Це НЕ міняє DB. DB міняється тільки через POST /api/settings/language.
    if requested_language:
        language = requested_language
    elif account:
        language = _normalize_language(getattr(account, "language", None) or cookie_language or DEFAULT_LANGUAGE)
    else:
        language = cookie_language or DEFAULT_LANGUAGE

    response = JSONResponse(_language_response_payload(language))
    response.set_cookie("alexihub_language", language, max_age=60 * 60 * 24 * 365, path="/", samesite="lax")
    return response


@router.post("/api/settings/language")
async def api_settings_language(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_authenticated"}, status_code=401)

    language = DEFAULT_LANGUAGE
    try:
        data = await request.json()
        language = data.get("language") or data.get("lang") or DEFAULT_LANGUAGE
    except Exception:
        form = await request.form()
        language = form.get("language") or form.get("lang") or DEFAULT_LANGUAGE

    normalized = _normalize_language(str(language))
    saved_language = await crud.update_account_language(db, account.id, normalized)
    final_language = saved_language or normalized
    response = JSONResponse(_language_response_payload(final_language))
    # Keep a lightweight client-side fallback too, so a refresh does not jump back
    # if the browser opens settings before the DB value is hydrated.
    response.set_cookie("alexihub_language", final_language, max_age=60 * 60 * 24 * 365, path="/", samesite="lax")
    return response


@router.post("/api/upload-image")
async def api_upload_image(request: Request, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_authenticated"}, status_code=401)
    prepared = await _read_profile_upload(file)
    if prepared is None:
        return JSONResponse({"ok": False, "error": "bad_file"}, status_code=400)
    data, content_type = prepared
    url = await _upload_to_cloudinary(data, content_type)
    if not url:
        url = await _upload_to_imgur(data, content_type)
    if not url:
        url = _save_profile_upload_local(data, content_type, account.id, "chat")
    return JSONResponse({"ok": True, "url": url})


async def server_rail_context(db: AsyncSession, account_id: int, active_server_id: int | None = None) -> dict:
    """Small shared context used by pages that show the Discord-style server rail."""
    return {
        "servers": await crud.list_servers_for_account(db, account_id),
        "active_server_id": active_server_id,
        "mention_counts": await crud.unread_mention_summary(db, account_id),
    }


# --- Registration -----------------------------------------------------------

@router.get("/register")
async def register_form(request: Request):
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "error": None, "bot_username": settings.bot_username},
    )


@router.post("/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    username = username.strip()
    email = email.strip().lower()

    def error(message: str, status_code: int = 400):
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": message, "bot_username": settings.bot_username},
            status_code=status_code,
        )

    if not username or not email or len(password) < 6:
        return error("Заповни всі поля (пароль — мінімум 6 символів).")

    if await crud.get_account_by_username(db, username):
        return error("Цей юзернейм вже зайнятий.")

    if await crud.get_account_by_email(db, email):
        return error("Акаунт з таким email вже існує.")

    account = await crud.create_account(
        db, username=username, email=email, password_hash=auth.hash_password(password)
    )
    auth.log_in(request, account.id)
    return RedirectResponse(url="/community", status_code=303)


# --- Login (email + Telegram) ------------------------------------------------

@router.get("/login")
async def login_form(request: Request, error: str | None = None):
    error_messages = {
        "telegram": "Не вдалось перевірити вхід через Telegram, спробуй ще раз.",
        "banned": "Цей акаунт заблоковано.",
    }
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": error_messages.get(error),
            "bot_username": settings.bot_username,
        },
    )


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    def error(message: str, status_code: int):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": message, "bot_username": settings.bot_username},
            status_code=status_code,
        )

    account = await crud.get_account_by_email(db, email.strip().lower())
    if not account or not account.password_hash or not auth.verify_password(password, account.password_hash):
        return error("Невірний email або пароль.", 401)

    if account.is_banned:
        return error("Цей акаунт заблоковано.", 403)

    auth.log_in(request, account.id)
    return RedirectResponse(url="/community", status_code=303)


@router.get("/telegram-callback")
async def telegram_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Telegram redirects the browser here (the widget's data-auth-url) with
    the signed login payload as query params after the user approves.
    """
    data = dict(request.query_params)

    if not auth.verify_telegram_login(data):
        return RedirectResponse(url="/community/login?error=telegram", status_code=303)

    telegram_id = int(data["id"])
    account = await crud.get_account_by_telegram_id(db, telegram_id)

    if account is None:
        # First time logging in with this Telegram account -- create one.
        # username must be unique, so fall back / disambiguate if needed.
        base_username = (data.get("username") or f"telegram_{telegram_id}").lower()
        username = base_username
        suffix = 1
        while await crud.get_account_by_username(db, username):
            suffix += 1
            username = f"{base_username}{suffix}"

        account = await crud.create_account(
            db,
            username=username,
            telegram_id=telegram_id,
            telegram_username=data.get("username"),
        )
        if data.get("photo_url"):
            account.avatar_url = data["photo_url"]
            await db.commit()

    if account.is_banned:
        return RedirectResponse(url="/community/login?error=banned", status_code=303)

    auth.log_in(request, account.id)
    return RedirectResponse(url="/community", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    auth.log_out(request)
    return RedirectResponse(url="/community/login", status_code=303)


# --- Main page: online members list -----------------------------------------

@router.get("")
@router.get("/")
async def community_home(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    await crud.touch_last_seen(db, account.id)
    await crud.ensure_default_channels(db)
    channels = await crud.list_channels(db)
    online_members = await crud.list_online_accounts(db)
    online_ids = [m.id for m in online_members]
    friends = await crud.list_friends(db, account.id)
    dm_threads = await crud.list_dm_threads_for_account(db, account.id)
    pending_incoming = await crud.list_pending_requests_with_requester(db, account.id)
    pending_outgoing = await crud.list_pending_sent_with_addressee(db, account.id)
    rail = await server_rail_context(db, account.id)

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "account": account,
            "online_members": online_members,
            "online_ids": online_ids,
            "channels": channels,
            "friends": friends,
            "dm_threads": dm_threads,
            "pending_incoming": pending_incoming,
            "pending_outgoing": pending_outgoing,
            **rail,
        },
    )


# --- Forum: channels, posts, likes, comments -------------------------------

@router.get("/channel/{slug}")
async def channel_view(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    await crud.touch_last_seen(db, account.id)
    await crud.ensure_default_channels(db)
    channel = await crud.get_channel_by_slug(db, slug)
    if not channel:
        return RedirectResponse(url="/community", status_code=303)

    channels = await crud.list_channels(db)
    feed = await crud.get_channel_feed(db, channel.id, viewer_id=account.id)
    rail = await server_rail_context(db, account.id)

    return templates.TemplateResponse(
        "channel.html",
        {"request": request, "account": account, "channels": channels, "channel": channel, "feed": feed, **rail},
    )


@router.post("/channel/{slug}/post")
async def channel_post_submit(
    slug: str, request: Request,
    content: str = Form(...), image_url: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    channel = await crud.get_channel_by_slug(db, slug)
    if channel and content.strip():
        await crud.create_post(db, channel.id, account.id, content.strip(), image_url.strip())
    return RedirectResponse(url=f"/community/channel/{slug}", status_code=303)


@router.post("/channel/{slug}/post/{post_id}/comment")
async def post_comment_submit(
    slug: str, post_id: int, request: Request, content: str = Form(...), db: AsyncSession = Depends(get_db)
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    if content.strip():
        await crud.add_comment(db, post_id, account.id, content.strip())
    return RedirectResponse(url=f"/community/channel/{slug}", status_code=303)


@router.post("/api/posts/{post_id}/like")
async def api_toggle_like(post_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)

    liked = await crud.toggle_like(db, post_id, account.id)
    count = await crud.count_likes(db, post_id)
    return JSONResponse({"liked": liked, "count": count})



# --- User servers: Discord-style private spaces -----------------------------

@router.get("/servers/new")
async def server_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    rail = await server_rail_context(db, account.id)
    return templates.TemplateResponse(
        "server_create.html",
        {
            "request": request,
            "account": account,
            "error": None,
            "join_error": None,
            "mode": "create",
            **rail,
        },
    )


@router.post("/servers/new")
async def server_create_submit(
    request: Request,
    name: str = Form(...),
    icon_url: str = Form(""),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    clean_name = name.strip()
    if len(clean_name) < 2:
        rail = await server_rail_context(db, account.id)
        return templates.TemplateResponse(
            "server_create.html",
            {
                "request": request,
                "account": account,
                "error": "Назва сервера мінімум 2 символи.",
                "join_error": None,
                "mode": "setup",
                **rail,
            },
            status_code=400,
        )

    server = await crud.create_server(
        db,
        owner_id=account.id,
        name=clean_name,
        icon_url=icon_url.strip(),
        description=description.strip(),
    )
    return RedirectResponse(url=f"/community/servers/{server.id}", status_code=303)


@router.post("/servers/join")
async def server_join_submit(
    request: Request,
    invite: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    raw_invite = (invite or "").strip()
    # Secure invite parser: accepts alexihub://server-invite/<code>,
    # /community/api/server-invites/respond/<code>, discord-like links ending in a code,
    # or just the random code itself. Direct server ids are no longer accepted.
    match = re.search(r"server-invite/([A-Za-z0-9_-]+)", raw_invite) or re.search(r"(?:invite|invites|respond|accept)/([A-Za-z0-9_-]+)", raw_invite)
    invite_code = match.group(1) if match else raw_invite.strip().split("/")[-1].split("?")[0]
    if not invite_code or not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", invite_code):
        rail = await server_rail_context(db, account.id)
        return templates.TemplateResponse(
            "server_create.html",
            {
                "request": request,
                "account": account,
                "error": None,
                "join_error": "Встав нормальний код або посилання-запрошення. Прямий ID сервера більше не працює.",
                "mode": "join",
                **rail,
            },
            status_code=400,
        )

    invite_row = await crud.accept_server_invite_by_code(db, invite_code, account.id)
    if not invite_row:
        rail = await server_rail_context(db, account.id)
        return templates.TemplateResponse(
            "server_create.html",
            {
                "request": request,
                "account": account,
                "error": None,
                "join_error": "Запрошення не знайдено, вже використане або не належить цьому акаунту.",
                "mode": "join",
                **rail,
            },
            status_code=404,
        )

    return RedirectResponse(url=f"/community/servers/{invite_row.server_id}", status_code=303)


@router.get("/servers/{server_id}")
async def server_home(server_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return _forbidden_response()

    await crud.touch_last_seen(db, account.id)
    server = await crud.get_server_by_id(db, server_id)
    if not server:
        return RedirectResponse(url="/community", status_code=303)

    channels = await crud.list_server_channels(db, server.id)
    members = await crud.list_server_members(db, server.id)
    friends = await crud.list_friends(db, account.id)
    rail = await server_rail_context(db, account.id, active_server_id=server.id)
    can_manage = await crud.can_manage_server(db, server.id, account.id)

    return templates.TemplateResponse(
        "server_home.html",
        {
            "request": request,
            "account": account,
            "server": server,
            "channels": channels,
            "members": members,
            "friends": friends,
            "can_manage": can_manage,
            **rail,
        },
    )


@router.post("/servers/{server_id}/channels/create")
async def server_channel_create_submit(
    server_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.can_manage_server(db, server_id, account.id):
        return RedirectResponse(url=f"/community/servers/{server_id}", status_code=303)

    new_channel = None
    if name.strip():
        new_channel = await crud.create_server_channel(db, server_id, name.strip(), description.strip())
    if new_channel:
        return RedirectResponse(url=f"/community/servers/{server_id}/channel/{new_channel.id}", status_code=303)
    return RedirectResponse(url=f"/community/servers/{server_id}", status_code=303)


@router.get("/servers/{server_id}/channel/{channel_id}")
async def server_channel_view(server_id: int, channel_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return _forbidden_response()

    await crud.touch_last_seen(db, account.id)
    server = await crud.get_server_by_id(db, server_id)
    channel = await crud.get_server_channel(db, server_id, channel_id)
    if not server or not channel:
        return RedirectResponse(url=f"/community/servers/{server_id}", status_code=303)

    channels = await crud.list_server_channels(db, server_id)
    members = await crud.list_server_members(db, server_id)
    friends = await crud.list_friends(db, account.id)
    feed = await crud.get_server_feed(db, server_id, channel_id)
    mentions_were_read = await crud.mark_server_channel_mentions_read(
        db, account.id, server_id, channel_id
    )
    rail = await server_rail_context(db, account.id, active_server_id=server_id)
    if mentions_were_read:
        await _broadcast_mention_counts([account.id])
    can_manage = await crud.can_manage_server(db, server_id, account.id)

    return templates.TemplateResponse(
        "server_channel.html",
        {
            "request": request,
            "account": account,
            "server": server,
            "channel": channel,
            "channels": channels,
            "members": members,
            "friends": friends,
            "can_manage": can_manage,
            "feed": feed,
            **rail,
        },
    )



@router.get("/servers/{server_id}/channel/{channel_id}/settings")
async def server_channel_settings_page(
    server_id: int,
    channel_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return _forbidden_response()
    if not await crud.can_manage_server(db, server_id, account.id):
        return RedirectResponse(url=f"/community/servers/{server_id}/channel/{channel_id}", status_code=303)

    server = await crud.get_server_by_id(db, server_id)
    channel = await crud.get_server_channel(db, server_id, channel_id)
    if not server or not channel:
        return RedirectResponse(url=f"/community/servers/{server_id}", status_code=303)

    channels = await crud.list_server_channels(db, server_id)
    rail = await server_rail_context(db, account.id, active_server_id=server_id)
    return templates.TemplateResponse(
        "channel_settings.html",
        {
            "request": request,
            "account": account,
            "server": server,
            "channel": channel,
            "channels": channels,
            **rail,
        },
    )


@router.post("/servers/{server_id}/channel/{channel_id}/settings")
async def server_channel_settings_submit(
    server_id: int,
    channel_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    redirect_to: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.can_manage_server(db, server_id, account.id):
        return RedirectResponse(url=f"/community/servers/{server_id}/channel/{channel_id}", status_code=303)

    if name.strip():
        await crud.update_server_channel(db, server_id, channel_id, name.strip(), description.strip())
    safe_redirect = redirect_to if redirect_to.startswith("/community/") else f"/community/servers/{server_id}/channel/{channel_id}/settings"
    return RedirectResponse(url=safe_redirect, status_code=303)


@router.post("/servers/{server_id}/channel/{channel_id}/delete")
async def server_channel_delete_submit(
    server_id: int,
    channel_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.can_manage_server(db, server_id, account.id):
        return RedirectResponse(url=f"/community/servers/{server_id}/channel/{channel_id}", status_code=303)

    await crud.delete_server_channel(db, server_id, channel_id)
    return RedirectResponse(url=f"/community/servers/{server_id}", status_code=303)


@router.post("/servers/{server_id}/channel/{channel_id}/message")
async def server_message_submit(
    server_id: int,
    channel_id: int,
    request: Request,
    content: str = Form(""),
    image_url: str = Form(""),
    reply_to_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return _forbidden_response()

    channel = await crud.get_server_channel(db, server_id, channel_id)
    content, image_url, _media_item = await _prepare_custom_media_message(
        db, account.id, content, image_url, context="server", server_id=server_id
    )
    realtime_payload = None
    if channel and (content.strip() or image_url.strip()):
        retry_after_ms = await message_rate_limiter.check(account.id)
        if retry_after_ms:
            return _message_rate_limit_redirect(
                f"/community/servers/{server_id}/channel/{channel_id}", retry_after_ms
            )
        reply_id = _parse_optional_int(reply_to_id)
        if reply_id:
            reply_msg = await crud.get_server_message(db, server_id, channel_id, reply_id)
            if not reply_msg:
                reply_id = None
        msg = await crud.create_server_message(db, server_id, channel_id, account.id, content.strip(), image_url.strip(), reply_to_id=reply_id)
        mention_affected = await _sync_server_message_mentions(db, msg)
        realtime_payload = await _server_message_realtime_event(db, msg)
        await _broadcast_mention_counts(mention_affected)
    if realtime_payload:
        # The HTML form is the WebSocket fallback. Other connected members must
        # still receive the message even though the sender is about to redirect.
        await realtime_channels.broadcast((server_id, channel_id), realtime_payload)
    return RedirectResponse(url=f"/community/servers/{server_id}/channel/{channel_id}", status_code=303)


@router.post("/servers/{server_id}/channel/{channel_id}/message/{message_id}/edit")
async def server_message_edit_submit(
    server_id: int,
    channel_id: int,
    message_id: int,
    request: Request,
    content: str = Form(""),
    image_url: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return _forbidden_response()

    message = await crud.get_server_message(db, server_id, channel_id, message_id)
    if message and message.author_id == account.id and not getattr(message, "is_forwarded", False) and content.strip():
        updated = await crud.update_server_message(db, message, content.strip(), image_url.strip())
        mention_affected = await _sync_server_message_mentions(db, updated)
        await _broadcast_mention_counts(mention_affected)
        await realtime_channels.broadcast(
            (server_id, channel_id),
            {
                "type": "message_edit",
                "message": {
                    "id": updated.id,
                    "content": updated.content,
                    "image_url": updated.image_url,
                    "edited_at": (updated.edited_at.isoformat() if getattr(updated, "edited_at", None) else None),
                },
            },
        )
    return RedirectResponse(url=f"/community/servers/{server_id}/channel/{channel_id}#message-{message_id}", status_code=303)


@router.post("/servers/{server_id}/channel/{channel_id}/message/{message_id}/delete")
async def server_message_delete_submit(
    server_id: int,
    channel_id: int,
    message_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return _forbidden_response()

    message = await crud.get_server_message(db, server_id, channel_id, message_id)
    if message and (message.author_id == account.id or await crud.can_manage_server(db, server_id, account.id)):
        mention_affected = await crud.delete_message_mentions(db, "server", message.id)
        await crud.delete_server_message(db, message)
        await _broadcast_mention_counts(mention_affected)
        await realtime_channels.broadcast(
            (server_id, channel_id),
            {"type": "message_delete", "message_id": int(message_id)},
        )
    return RedirectResponse(url=f"/community/servers/{server_id}/channel/{channel_id}", status_code=303)



@router.get("/servers/{server_id}/settings")
async def server_settings_page(
    server_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return _forbidden_response()
    if not await crud.can_manage_server(db, server_id, account.id):
        return RedirectResponse(url=f"/community/servers/{server_id}", status_code=303)

    server = await crud.get_server_by_id(db, server_id)
    if not server:
        return RedirectResponse(url="/community", status_code=303)
    members = await crud.list_server_members(db, server_id)
    banner_color = await _get_server_banner_color(db, server_id)
    rail = await server_rail_context(db, account.id, active_server_id=server_id)
    return templates.TemplateResponse(
        "server_settings.html",
        {
            "request": request,
            "account": account,
            "server": server,
            "members_count": len(members),
            "banner_color": banner_color,
            **rail,
        },
    )

@router.post("/servers/{server_id}/settings")
async def server_settings_submit(
    server_id: int,
    request: Request,
    name: str = Form(...),
    icon_url: str = Form(""),
    description: str = Form(""),
    banner_color: str = Form(""),
    redirect_to: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.can_manage_server(db, server_id, account.id):
        return RedirectResponse(url=f"/community/servers/{server_id}", status_code=303)

    if name.strip():
        await crud.update_server_settings(db, server_id, name.strip(), icon_url.strip(), description.strip())
        await _set_server_banner_color(db, server_id, banner_color)
    safe_redirect = redirect_to if redirect_to.startswith("/community/") else f"/community/servers/{server_id}"
    return RedirectResponse(url=safe_redirect, status_code=303)


@router.post("/servers/{server_id}/leave")
async def server_leave_submit(
    server_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    await crud.leave_server(db, server_id, account.id)
    return RedirectResponse(url="/community", status_code=303)


@router.post("/servers/{server_id}/invite")
async def server_invite_submit(
    server_id: int,
    request: Request,
    username: str = Form(...),
    channel_id: str = Form(""),
    redirect_to: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return _forbidden_response()

    target = await crud.get_account_by_username(db, username.strip())
    if target:
        # Capture primitive values BEFORE helper functions commit. AsyncSession can
        # expire ORM instances on commit; touching account.id/invite.id after that
        # is exactly how MissingGreenlet can appear on Render/asyncpg.
        account_id = int(account.id)
        target_id = int(target.id)
        author_payload = _account_payload(account)

        retry_after_ms = await message_rate_limiter.check(account_id)
        if retry_after_ms:
            safe_redirect = redirect_to if redirect_to.startswith("/community/") else f"/community/servers/{server_id}"
            return _message_rate_limit_redirect(safe_redirect, retry_after_ms)

        invite = await crud.invite_friend_to_server(db, server_id, account_id, target_id)
        if invite:
            invite_code = str(getattr(invite, "code", None) or invite.id)
            invite_channel_id = _parse_optional_int(channel_id)
            if invite_channel_id:
                channel = await crud.get_server_channel(db, server_id, invite_channel_id)
                if not channel:
                    invite_channel_id = None

            # Keep the database message as plain text. The DM page will render
            # the Discord-like card client-side through /api/.../preview.
            # This makes old/broken invite rows unable to crash the whole DM page.
            thread = await crud.get_or_create_dm_thread(db, account_id, target_id)
            if thread:
                thread_id = int(thread.id)
                content = crud.make_server_invite_dm_content(invite_code, invite_channel_id)
                msg = await crud.create_dm_message(db, thread_id, account_id, content)
                mention_affected = await _sync_dm_message_mentions(db, msg)
                await _broadcast_mention_counts(mention_affected)
                message_id = int(msg.id)
                created_at = msg.created_at.isoformat()
                await realtime_channels.broadcast(
                    (0, thread_id),
                    {
                        "type": "message",
                        "message": {
                            "id": message_id,
                            "thread_id": thread_id,
                            "author_id": account_id,
                            "content": content,
                            "image_url": None,
                            "created_at": created_at,
                            "reply_to_id": None,
                            "reply": None,
                        },
                        "author": author_payload,
                    },
                )
                await _emit_dm_sidebar_update(thread_id, message_id)
    safe_redirect = redirect_to if redirect_to.startswith("/community/") else f"/community/servers/{server_id}"
    return RedirectResponse(url=safe_redirect, status_code=303)





@router.get("/api/forward-targets")
async def api_forward_targets(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"error": "not_logged_in", "dms": [], "channels": []}, status_code=401)

    # Keep this endpoint bulletproof: the forward modal should never break the page.
    # The real bug was an AttributeError in crud.list_forward_targets() when an
    # Account has no display_name column/attribute. If another edge case appears,
    # return an empty list and print traceback to Render logs instead of throwing
    # an HTML 500 that makes fetch().json() explode.
    try:
        account_id = int(account.id)
        targets = await crud.list_forward_targets(db, account_id)
        if not isinstance(targets, dict):
            targets = {"dms": [], "channels": []}
        targets.setdefault("dms", [])
        targets.setdefault("channels", [])
        return JSONResponse(targets)
    except Exception as exc:
        import traceback
        print("[forward-targets] failed:", repr(exc))
        traceback.print_exc()
        return JSONResponse({"dms": [], "channels": [], "error": "forward_targets_failed"}, status_code=200)


@router.post("/api/messages/forward")
async def api_forward_message(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}

    source_type = str(body.get("source_type") or body.get("sourceType") or "").strip().lower()
    message_id = _parse_optional_int(body.get("message_id") or body.get("messageId"))
    targets = body.get("targets") if isinstance(body.get("targets"), list) else []

    if source_type not in {"dm", "server"} or not message_id:
        return JSONResponse({"ok": False, "error": "bad_source"}, status_code=400)
    if not targets:
        return JSONResponse({"ok": False, "error": "no_targets"}, status_code=400)

    source_content = ""
    source_image_url = ""

    if source_type == "dm":
        source = await crud.get_dm_message_by_id(db, message_id)
        if not source or not await crud.is_dm_participant(db, source.thread_id, account.id):
            return JSONResponse({"ok": False, "error": "source_not_found"}, status_code=404)
        source_content = source.content or ""
        source_image_url = source.image_url or ""
    else:
        source = await crud.get_server_message_by_id(db, message_id)
        if not source or not await crud.is_server_member(db, source.server_id, account.id):
            return JSONResponse({"ok": False, "error": "source_not_found"}, status_code=404)
        source_content = source.content or ""
        source_image_url = source.image_url or ""

    if crud.parse_nitro_dm_gift_marker(source_content):
        return JSONResponse({
            "ok": False,
            "error": "nitro_gift_not_forwardable",
            "message": "Nitro-подарок нельзя пересылать: он привязан к получателю.",
        }, status_code=400)

    if not source_content.strip() and not source_image_url.strip():
        return JSONResponse({"ok": False, "error": "empty_source"}, status_code=400)

    # Store primitive values before commits to avoid MissingGreenlet surprises
    # when SQLAlchemy expires ORM attributes after a commit.
    account_id = int(account.id)
    author_payload = _account_payload(account)
    sent = []
    seen_targets = set()

    for raw_target in targets[:25]:
        if not isinstance(raw_target, dict):
            continue
        target_type = str(raw_target.get("type") or "").strip().lower()

        if target_type == "dm":
            username = str(raw_target.get("username") or "").strip()
            if not username:
                continue
            dedupe_key = ("dm", username.lower())
            if dedupe_key in seen_targets:
                continue
            seen_targets.add(dedupe_key)

            target_account = await crud.get_account_by_username(db, username)
            if not target_account or target_account.id == account_id:
                continue
            # Forwarding to a DM is intentionally limited to friends, like the modal list.
            if await crud.friendship_status(db, account_id, target_account.id) != "friends":
                continue

            thread = await crud.get_or_create_dm_thread(db, account_id, target_account.id)
            if not thread:
                continue
            thread_id = int(thread.id)
            retry_after_ms = await message_rate_limiter.check(account_id)
            if retry_after_ms:
                return _message_rate_limit_json_response(retry_after_ms, sent=sent)
            msg = await crud.create_dm_message(db, thread_id, account_id, source_content, source_image_url, is_forwarded=True)
            mention_affected = await _sync_dm_message_mentions(db, msg)
            await _broadcast_mention_counts(mention_affected)
            message_payload = {
                "id": int(msg.id),
                "thread_id": thread_id,
                "author_id": account_id,
                "content": msg.content,
                "image_url": msg.image_url,
                "created_at": msg.created_at.isoformat(),
                "reply_to_id": None,
                "reply": None,
                "is_forwarded": True,
            }
            await realtime_channels.broadcast((0, thread_id), {"type": "message", "message": message_payload, "author": author_payload})
            await _emit_dm_sidebar_update(thread_id, int(msg.id))
            sent.append({"type": "dm", "username": username})
            continue

        if target_type == "channel":
            server_id = _parse_optional_int(raw_target.get("server_id"))
            channel_id = _parse_optional_int(raw_target.get("channel_id"))
            if not server_id or not channel_id:
                continue
            dedupe_key = ("channel", int(server_id), int(channel_id))
            if dedupe_key in seen_targets:
                continue
            seen_targets.add(dedupe_key)

            if not await crud.is_server_member(db, server_id, account_id):
                continue
            channel = await crud.get_server_channel(db, server_id, channel_id)
            if not channel:
                continue

            retry_after_ms = await message_rate_limiter.check(account_id)
            if retry_after_ms:
                return _message_rate_limit_json_response(retry_after_ms, sent=sent)
            msg = await crud.create_server_message(db, server_id, channel_id, account_id, source_content, source_image_url, is_forwarded=True)
            mention_affected = await _sync_server_message_mentions(db, msg)
            await _broadcast_mention_counts(mention_affected)
            message_payload = {
                "id": int(msg.id),
                "server_id": int(server_id),
                "channel_id": int(channel_id),
                "author_id": account_id,
                "content": msg.content,
                "image_url": msg.image_url,
                "created_at": msg.created_at.isoformat(),
                "reply_to_id": None,
                "reply": None,
                "is_forwarded": True,
            }
            await realtime_channels.broadcast((server_id, channel_id), {"type": "message", "message": message_payload, "author": author_payload})
            sent.append({"type": "channel", "server_id": server_id, "channel_id": channel_id})

    return JSONResponse({"ok": True, "sent": sent, "count": len(sent)})


@router.get("/api/server-invites/{invite_code}/preview")
async def api_server_invite_preview(invite_code: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)
    channel_id = _parse_optional_int(request.query_params.get("channel_id"))
    payload = await crud.build_server_invite_preview(db, invite_code, viewer.id, channel_id)
    return JSONResponse(payload)


@router.post("/api/server-invites/accept/{invite_code}")
async def api_accept_server_invite_by_code(invite_code: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)
    body = await request.json() if request.headers.get("content-type", "").lower().startswith("application/json") else {}
    requested_channel_id = _parse_optional_int(body.get("channel_id"))
    invite = await crud.accept_server_invite_by_code(db, invite_code, viewer.id)
    if not invite:
        return JSONResponse({"error": "not_found_or_used"}, status_code=404)

    channel_id = None
    if requested_channel_id:
        ch = await crud.get_server_channel(db, invite.server_id, requested_channel_id)
        if ch:
            channel_id = ch.id
    return JSONResponse({"status": "accepted", "server_id": invite.server_id, "channel_id": channel_id})


@router.post("/api/server-invites/respond/{invite_code}")
async def api_respond_server_invite(invite_code: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)

    body = await request.json()
    accept = bool(body.get("accept"))
    requested_channel_id = _parse_optional_int(body.get("channel_id"))
    invite = await crud.respond_server_invite(db, invite_code, viewer.id, accept)
    if not invite:
        return JSONResponse({"error": "not_found_or_used"}, status_code=404)

    channel_id = None
    if accept and requested_channel_id:
        ch = await crud.get_server_channel(db, invite.server_id, requested_channel_id)
        if ch:
            channel_id = ch.id

    return JSONResponse({"status": invite.status, "server_id": invite.server_id, "channel_id": channel_id})








@router.get("/api/nitro/me")
async def api_nitro_me(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    profile_nitro = await crud.nitro_profile_payload(db, account.id)
    return JSONResponse({
        "ok": True,
        "subscription": profile_nitro,
        "verified": bool(getattr(account, "is_verified", False)),
        "can_generate": crud.is_nitro_code_generator(account),
    })


@router.post("/api/nitro/redeem")
async def api_nitro_redeem(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    code = ""
    try:
        body = await request.json()
        code = str(body.get("code") or "")
    except Exception:
        try:
            form = await request.form()
            code = str(form.get("code") or "")
        except Exception:
            code = ""
    result = await crud.redeem_nitro_gift_code(db, account.id, code)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/nitro/generate")
async def api_nitro_generate(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    if not crud.is_nitro_code_generator(account):
        return JSONResponse({"ok": False, "error": "forbidden", "message": "Генерация доступна только CODE/admin."}, status_code=403)
    days = 30
    note = ""
    try:
        body = await request.json()
        days = int(body.get("days") or 30)
        note = str(body.get("note") or "")
    except Exception:
        try:
            form = await request.form()
            days = int(form.get("days") or 30)
            note = str(form.get("note") or "")
        except Exception:
            pass
    code = await crud.create_nitro_gift_code(db, account.id, days=days, note=note)
    return JSONResponse({"ok": True, "code": code})


@router.get("/api/users/{username}/nitro")
async def api_user_nitro(username: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    account = await crud.get_account_by_username(db, username)
    if not account:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    payload = await crud.nitro_profile_payload(db, account.id)
    return JSONResponse({
        "ok": True,
        "username": account.username,
        "verified": bool(getattr(account, "is_verified", False)),
        "nitro": payload,
    })


@router.post("/api/dm/{username}/nitro-gifts")
async def api_send_dm_nitro_gift(username: str, request: Request, db: AsyncSession = Depends(get_db)):
    sender = await current_account(request, db)
    if not sender:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    if not crud.is_nitro_code_generator(sender):
        return JSONResponse({
            "ok": False,
            "error": "forbidden",
            "message": "Упаковывать Nitro могут только верифицированные участники и команда.",
        }, status_code=403)

    recipient = await crud.get_account_by_username(db, username)
    if not recipient or recipient.id == sender.id:
        return JSONResponse({"ok": False, "error": "recipient_not_found", "message": "Получатель не найден."}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        days = int(body.get("days") or 30)
    except Exception:
        days = 30
    note = str(body.get("note") or "")

    sender_id = int(sender.id)
    recipient_id = int(recipient.id)
    author_payload = _account_payload(sender)
    thread = await crud.get_or_create_dm_thread(db, sender_id, recipient_id)
    if not thread:
        return JSONResponse({"ok": False, "error": "thread_not_found", "message": "Не удалось открыть ЛС."}, status_code=400)
    thread_id = int(thread.id)

    result = await crud.create_nitro_dm_gift(
        db,
        thread_id=thread_id,
        sender_id=sender_id,
        recipient_id=recipient_id,
        days=days,
        note=note,
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)

    message_payload = result.get("message") or {}
    gift_payload = result.get("gift") or {}
    message_payload["nitro_gift"] = gift_payload
    await realtime_channels.clear_typing((0, thread_id), sender_id)
    await realtime_channels.broadcast(
        (0, thread_id),
        {"type": "message", "message": message_payload, "author": author_payload},
    )
    gift_message_id = _parse_optional_int(message_payload.get("id"))
    if gift_message_id:
        await _emit_dm_sidebar_update(thread_id, gift_message_id)
    return JSONResponse({"ok": True, "message": message_payload, "gift": gift_payload})


@router.get("/api/nitro/dm-gifts/{public_token}")
async def api_get_dm_nitro_gift(public_token: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    gift = await crud.get_nitro_dm_gift(db, public_token, viewer.id)
    if not gift:
        return JSONResponse({"ok": False, "error": "not_found", "message": "Подарок не найден."}, status_code=404)
    return JSONResponse({"ok": True, "gift": gift})


@router.post("/api/nitro/dm-gifts/{public_token}/claim")
async def api_claim_dm_nitro_gift(public_token: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    result = await crud.claim_nitro_dm_gift(db, public_token, viewer.id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    gift = result.get("gift") or {}
    thread_id = gift.get("thread_id")
    if thread_id:
        await realtime_channels.broadcast(
            (0, int(thread_id)),
            {"type": "nitro_gift_update", "gift": gift},
        )
    return JSONResponse(result)

@router.get("/api/dm/{username}/pins")
async def api_list_dm_pins(username: str, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in", "pins": []}, status_code=401)
    other = await crud.get_account_by_username(db, username)
    if not other:
        return JSONResponse({"ok": False, "error": "not_found", "pins": []}, status_code=404)
    thread = await crud.get_or_create_dm_thread(db, account.id, other.id)
    if not thread or not await crud.is_dm_participant(db, thread.id, account.id):
        return JSONResponse({"ok": False, "error": "forbidden", "pins": []}, status_code=403)
    pins = await crud.list_dm_pins(db, thread.id)
    return JSONResponse({"ok": True, "pins": pins})


@router.post("/api/dm/{username}/pins/{message_id}")
async def api_pin_dm_message(username: str, message_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    other = await crud.get_account_by_username(db, username)
    if not other:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    thread = await crud.get_or_create_dm_thread(db, account.id, other.id)
    if not thread or not await crud.is_dm_participant(db, thread.id, account.id):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    pin, created = await crud.pin_dm_message(db, thread.id, message_id, account.id)
    if not pin:
        return JSONResponse({"ok": False, "error": "message_not_found"}, status_code=404)
    payload = {"type": "pin_add", "pin": pin, "actor": _account_payload(account), "created": created}
    await realtime_channels.broadcast((0, int(thread.id)), payload)
    return JSONResponse({"ok": True, "created": created, "pin": pin})


@router.delete("/api/dm/{username}/pins/{message_id}")
async def api_unpin_dm_message(username: str, message_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    other = await crud.get_account_by_username(db, username)
    if not other:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    thread = await crud.get_or_create_dm_thread(db, account.id, other.id)
    if not thread or not await crud.is_dm_participant(db, thread.id, account.id):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    removed = await crud.unpin_dm_message(db, thread.id, message_id)
    if removed:
        await realtime_channels.broadcast((0, int(thread.id)), {"type": "pin_remove", "message_id": int(message_id), "actor": _account_payload(account)})
    return JSONResponse({"ok": True, "removed": bool(removed), "message_id": int(message_id)})


@router.get("/api/servers/{server_id}/channels/{channel_id}/pins")
async def api_list_server_pins(server_id: int, channel_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in", "pins": []}, status_code=401)
    if not await crud.is_server_member(db, server_id, account.id):
        return JSONResponse({"ok": False, "error": "forbidden", "pins": []}, status_code=403)
    channel = await crud.get_server_channel(db, server_id, channel_id)
    if not channel:
        return JSONResponse({"ok": False, "error": "not_found", "pins": []}, status_code=404)
    pins = await crud.list_server_pins(db, server_id, channel_id)
    return JSONResponse({"ok": True, "pins": pins})


@router.post("/api/servers/{server_id}/channels/{channel_id}/pins/{message_id}")
async def api_pin_server_message(server_id: int, channel_id: int, message_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    if not await crud.is_server_member(db, server_id, account.id):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    channel = await crud.get_server_channel(db, server_id, channel_id)
    if not channel:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    pin, created = await crud.pin_server_message(db, server_id, channel_id, message_id, account.id)
    if not pin:
        return JSONResponse({"ok": False, "error": "message_not_found"}, status_code=404)
    payload = {"type": "pin_add", "pin": pin, "actor": _account_payload(account), "created": created}
    await realtime_channels.broadcast((int(server_id), int(channel_id)), payload)
    return JSONResponse({"ok": True, "created": created, "pin": pin})


@router.delete("/api/servers/{server_id}/channels/{channel_id}/pins/{message_id}")
async def api_unpin_server_message(server_id: int, channel_id: int, message_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    if not await crud.is_server_member(db, server_id, account.id):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    channel = await crud.get_server_channel(db, server_id, channel_id)
    if not channel:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    removed = await crud.unpin_server_message(db, server_id, channel_id, message_id)
    if removed:
        await realtime_channels.broadcast((int(server_id), int(channel_id)), {"type": "pin_remove", "message_id": int(message_id), "actor": _account_payload(account)})
    return JSONResponse({"ok": True, "removed": bool(removed), "message_id": int(message_id)})




# --- Message reactions -------------------------------------------------------

def _parse_reaction_message_ids(raw: str | None) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for part in (raw or "").split(","):
        try:
            value = int(part.strip())
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        result.append(value)
        seen.add(value)
        if len(result) >= 100:
            break
    return result


def _reaction_status(error: str | None) -> int:
    if error == "nitro_required":
        return 403
    if error in {"emoji_unavailable", "not_found"}:
        return 404
    return 400


@router.get("/api/dm/{username}/reactions")
async def api_dm_reactions(
    username: str,
    request: Request,
    message_ids: str = "",
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in", "reactions": {}}, status_code=401)
    other = await crud.get_account_by_username(db, username)
    if not other:
        return JSONResponse({"ok": False, "error": "not_found", "reactions": {}}, status_code=404)
    thread = await crud.get_dm_thread_between(db, account.id, other.id)
    if not thread:
        return JSONResponse({"ok": True, "reactions": {}})
    summaries = await crud.list_dm_reaction_summaries(
        db,
        int(thread.id),
        _parse_reaction_message_ids(message_ids),
        int(account.id),
    )
    return JSONResponse({"ok": True, "reactions": {str(key): value for key, value in summaries.items()}})


@router.post("/api/dm/{username}/messages/{message_id}/reactions/toggle")
async def api_toggle_dm_reaction(
    username: str,
    message_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    other = await crud.get_account_by_username(db, username)
    if not other:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    thread = await crud.get_dm_thread_between(db, account.id, other.id)
    if not thread or not await crud.get_dm_message(db, int(thread.id), int(message_id)):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    result = await crud.toggle_message_reaction(
        db,
        context="dm",
        message_id=int(message_id),
        account_id=int(account.id),
        emoji_kind=str(body.get("kind") or "unicode"),
        emoji_value=str(body.get("value") or ""),
        custom_emoji_id=_parse_optional_int(body.get("custom_emoji_id")),
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=_reaction_status(result.get("error")))
    realtime_payload = {
        "type": "reaction_update",
        "message_id": int(message_id),
    }
    await realtime_channels.broadcast((0, int(thread.id)), realtime_payload)
    return JSONResponse({
        "ok": True,
        "type": "reaction_update",
        "message_id": int(message_id),
        "reactions": result.get("reactions") or [],
        "added": bool(result.get("added")),
    })


@router.get("/api/servers/{server_id}/channel/{channel_id}/reactions")
async def api_server_reactions(
    server_id: int,
    channel_id: int,
    request: Request,
    message_ids: str = "",
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in", "reactions": {}}, status_code=401)
    if not await crud.is_server_member(db, server_id, account.id):
        return JSONResponse({"ok": False, "error": "forbidden", "reactions": {}}, status_code=403)
    channel = await crud.get_server_channel(db, server_id, channel_id)
    if not channel:
        return JSONResponse({"ok": False, "error": "not_found", "reactions": {}}, status_code=404)
    summaries = await crud.list_server_reaction_summaries(
        db,
        server_id,
        channel_id,
        _parse_reaction_message_ids(message_ids),
        int(account.id),
    )
    return JSONResponse({"ok": True, "reactions": {str(key): value for key, value in summaries.items()}})


@router.post("/api/servers/{server_id}/channel/{channel_id}/messages/{message_id}/reactions/toggle")
async def api_toggle_server_reaction(
    server_id: int,
    channel_id: int,
    message_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)
    if not await crud.is_server_member(db, server_id, account.id):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    message = await crud.get_server_message(db, server_id, channel_id, message_id)
    if not message:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    result = await crud.toggle_message_reaction(
        db,
        context="server",
        message_id=int(message_id),
        account_id=int(account.id),
        emoji_kind=str(body.get("kind") or "unicode"),
        emoji_value=str(body.get("value") or ""),
        custom_emoji_id=_parse_optional_int(body.get("custom_emoji_id")),
        current_server_id=int(server_id),
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=_reaction_status(result.get("error")))
    realtime_payload = {
        "type": "reaction_update",
        "message_id": int(message_id),
    }
    await realtime_channels.broadcast((int(server_id), int(channel_id)), realtime_payload)
    return JSONResponse({
        "ok": True,
        "type": "reaction_update",
        "message_id": int(message_id),
        "reactions": result.get("reactions") or [],
        "added": bool(result.get("added")),
    })


_MEDIA_SEND_RE = re.compile(r"^\s*\[\[ah:(emoji|sticker):(\d+)\]\]\s*$")
_MEDIA_TOKEN_RE = re.compile(r"\[\[ah:(emoji|sticker):(\d+)\]\]")

async def _prepare_custom_media_message(
    db: AsyncSession,
    account_id: int,
    content: str,
    image_url: str,
    *,
    context: str,
    server_id: int | None = None,
) -> tuple[str, str, dict | None]:
    """Normalize custom emoji/sticker messages and enforce Nitro/server-scope rules."""
    clean_content = (content or "").strip()
    clean_image = (image_url or "").strip()
    matches = list(_MEDIA_TOKEN_RE.finditer(clean_content))
    if not matches:
        return clean_content, clean_image, None

    exact = _MEDIA_SEND_RE.match(clean_content)
    # Stickers are standalone messages. Keeping that invariant avoids a hidden
    # image URL being attached to arbitrary text or to multiple sticker tokens.
    if any(match.group(1) == "sticker" for match in matches) and not (
        exact and exact.group(1) == "sticker" and len(matches) == 1
    ):
        return "", "", None

    resolved: dict | None = None
    checked: set[tuple[str, int]] = set()
    for match in matches:
        kind = match.group(1)
        item_id = int(match.group(2))
        key = (kind, item_id)
        if key in checked:
            continue
        checked.add(key)
        item = await crud.get_media_item_for_send(
            db,
            account_id=int(account_id),
            kind=kind,
            item_id=item_id,
            current_server_id=server_id if context == "server" else None,
            context=context,
        )
        if not item or not item.get("allowed"):
            return "", "", None
        resolved = resolved or item

    if exact and exact.group(1) == "sticker":
        sticker_id = int(exact.group(2))
        return f"[[ah:sticker:{sticker_id}]]", (resolved or {}).get("image_url") or "", resolved

    # Persist the unambiguous IDs. The client renders these markers as images;
    # unlike :name: codes, same-named emoji from different servers cannot clash.
    return clean_content, clean_image, resolved

# --- Custom server emoji/sticker media --------------------------------------
async def _media_upload_url(file: UploadFile | None, account_id: int, kind: str) -> tuple[str | None, str | None]:
    prepared = await _read_profile_upload(file)
    if not prepared:
        return None, None
    data, content_type = prepared
    url = await _upload_to_cloudinary(data, content_type)
    if not url:
        url = await _upload_to_imgur(data, content_type)
    if not url:
        url = _save_profile_upload_local(data, content_type, account_id, kind)
    return url, content_type

@router.get('/api/servers/{server_id}/emojis')
async def api_list_server_emojis(server_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account=await current_account(request, db)
    if not account: return JSONResponse({'ok':False,'error':'not_logged_in','emojis':[]}, status_code=401)
    if not await crud.is_server_member(db, server_id, account.id): return JSONResponse({'ok':False,'error':'forbidden','emojis':[]}, status_code=403)
    return JSONResponse({'ok':True,'emojis':await crud.list_server_emojis(db, server_id)})

@router.post('/api/servers/{server_id}/emojis')
async def api_create_server_emoji(server_id:int, request:Request, name:str=Form(...), file:UploadFile=File(...), db:AsyncSession=Depends(get_db)):
    account=await current_account(request, db)
    if not account: return JSONResponse({'ok':False,'error':'not_logged_in'}, status_code=401)
    if not await crud.can_manage_server(db, server_id, account.id): return JSONResponse({'ok':False,'error':'forbidden'}, status_code=403)
    url,ct=await _media_upload_url(file, account.id, 'emoji')
    if not url: return JSONResponse({'ok':False,'error':'bad_file','message':'Потрібен PNG/JPG/GIF/WebP до 5 MB.'}, status_code=400)
    return JSONResponse({'ok':True,'emoji':await crud.create_server_emoji(db, server_id, name, url, ct, account.id)})

@router.delete('/api/servers/{server_id}/emojis/{emoji_id}')
async def api_delete_server_emoji(server_id:int, emoji_id:int, request:Request, db:AsyncSession=Depends(get_db)):
    account=await current_account(request, db)
    if not account: return JSONResponse({'ok':False,'error':'not_logged_in'}, status_code=401)
    if not await crud.can_manage_server(db, server_id, account.id): return JSONResponse({'ok':False,'error':'forbidden'}, status_code=403)
    return JSONResponse({'ok':True,'removed':await crud.delete_server_emoji(db, server_id, emoji_id)})

@router.get('/api/servers/{server_id}/stickers')
async def api_list_server_stickers(server_id:int, request:Request, db:AsyncSession=Depends(get_db)):
    account=await current_account(request, db)
    if not account: return JSONResponse({'ok':False,'error':'not_logged_in','stickers':[]}, status_code=401)
    if not await crud.is_server_member(db, server_id, account.id): return JSONResponse({'ok':False,'error':'forbidden','stickers':[]}, status_code=403)
    return JSONResponse({'ok':True,'stickers':await crud.list_server_stickers(db, server_id)})

@router.post('/api/servers/{server_id}/stickers')
async def api_create_server_sticker(server_id:int, request:Request, name:str=Form(...), description:str=Form(''), emoji:str=Form(''), file:UploadFile=File(...), db:AsyncSession=Depends(get_db)):
    account=await current_account(request, db)
    if not account: return JSONResponse({'ok':False,'error':'not_logged_in'}, status_code=401)
    if not await crud.can_manage_server(db, server_id, account.id): return JSONResponse({'ok':False,'error':'forbidden'}, status_code=403)
    url,ct=await _media_upload_url(file, account.id, 'sticker')
    if not url: return JSONResponse({'ok':False,'error':'bad_file','message':'Потрібен APNG/JPG/PNG/GIF/WebP до 5 MB.'}, status_code=400)
    return JSONResponse({'ok':True,'sticker':await crud.create_server_sticker(db, server_id, name, description, emoji, url, ct, account.id)})

@router.delete('/api/servers/{server_id}/stickers/{sticker_id}')
async def api_delete_server_sticker(server_id:int, sticker_id:int, request:Request, db:AsyncSession=Depends(get_db)):
    account=await current_account(request, db)
    if not account: return JSONResponse({'ok':False,'error':'not_logged_in'}, status_code=401)
    if not await crud.can_manage_server(db, server_id, account.id): return JSONResponse({'ok':False,'error':'forbidden'}, status_code=403)
    return JSONResponse({'ok':True,'removed':await crud.delete_server_sticker(db, server_id, sticker_id)})

@router.get('/api/media/library')
async def api_media_library(
    request: Request,
    context: str = 'server',
    server_id: int | None = None,
    channel_id: int | None = None,
    thread_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return JSONResponse(
            {'ok': False, 'error': 'not_logged_in', 'emojis': [], 'stickers': []},
            status_code=401,
        )

    clean_context = 'dm' if context == 'dm' else 'server'
    if clean_context == 'server' and server_id:
        if not await crud.is_server_member(db, server_id, account.id):
            return JSONResponse(
                {'ok': False, 'error': 'forbidden', 'emojis': [], 'stickers': []},
                status_code=403,
            )
        if channel_id and not await crud.get_server_channel(db, server_id, channel_id):
            return JSONResponse(
                {'ok': False, 'error': 'channel_not_found', 'emojis': [], 'stickers': []},
                status_code=404,
            )
    if clean_context == 'dm' and thread_id:
        if not await crud.is_dm_participant(db, thread_id, account.id):
            return JSONResponse(
                {'ok': False, 'error': 'forbidden', 'emojis': [], 'stickers': []},
                status_code=403,
            )

    lib = await crud.media_library_for_account(
        db, account.id, current_server_id=server_id, context=clean_context
    )
    referenced = await crud.list_referenced_message_emojis(
        db,
        context=clean_context,
        thread_id=thread_id,
        server_id=server_id,
        channel_id=channel_id,
    )
    known_ids = {int(item.get('id') or 0) for item in lib.get('emojis', [])}
    lib.setdefault('emojis', []).extend(
        item for item in referenced if int(item.get('id') or 0) not in known_ids
    )
    lib['ok'] = True
    return JSONResponse(lib)

@router.websocket("/ws/servers/{server_id}/channel/{channel_id}")
async def ws_server_channel(websocket: WebSocket, server_id: int, channel_id: int):
    """Realtime server channel: messages + typing indicator.

    Optimized for the free PostgreSQL tier:
    - typing is RAM-only and never touches the DB;
    - no message polling loop;
    - DB write happens only once when the user sends a real message.
    """
    account_id = _ws_account_id(websocket)
    if not account_id:
        await websocket.close(code=1008)
        return

    async with AsyncSessionLocal() as db:
        account = await crud.get_account_by_id(db, account_id)
        channel = await crud.get_server_channel(db, server_id, channel_id)
        is_member = await crud.is_server_member(db, server_id, account_id)
        if not account or not channel or not is_member:
            await websocket.close(code=1008)
            return
        profile = _account_payload(account)

    key = (server_id, channel_id)
    await realtime_channels.connect(key, account_id, websocket, profile)

    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            event_type = str(data.get("type") or "").strip().lower()

            if event_type in {"leave", "disconnect", "close"}:
                break

            if event_type == "sync":
                try:
                    after_id = max(0, int(data.get("after_id") or 0))
                except Exception:
                    after_id = 0
                async with AsyncSessionLocal() as db:
                    if not await crud.is_server_member(db, server_id, account_id):
                        await websocket.close(code=1008)
                        return
                    missed = await crud.list_server_messages_after(
                        db, server_id, channel_id, after_id, limit=201
                    )
                    has_more = len(missed) > 200
                    events = [
                        await _server_message_realtime_event(db, message)
                        for message in missed[:200]
                    ]
                await websocket.send_json(
                    {"type": "message_sync", "events": events, "has_more": has_more}
                )
                continue

            if event_type == "typing":
                await realtime_channels.set_typing(key, account_id, profile)
                continue

            if event_type == "typing_stop":
                await realtime_channels.clear_typing(key, account_id)
                continue

            if event_type == "edit":
                try:
                    edit_id = int(data.get("id") or data.get("message_id") or 0)
                except Exception:
                    edit_id = 0
                content = (data.get("content") or "").strip()
                image_url = (data.get("image_url") or "").strip()
                if not edit_id or not content:
                    continue
                if len(content) > 4000:
                    content = content[:4000]
                if len(image_url) > 512:
                    image_url = image_url[:512]

                async with AsyncSessionLocal() as db:
                    if not await crud.is_server_member(db, server_id, account_id):
                        await websocket.close(code=1008)
                        return
                    message = await crud.get_server_message(db, server_id, channel_id, edit_id)
                    if not message or message.author_id != account_id or getattr(message, "is_forwarded", False):
                        continue
                    updated = await crud.update_server_message(db, message, content, image_url)
                    mention_affected = await _sync_server_message_mentions(db, updated)
                    payload = {
                        "id": updated.id,
                        "content": updated.content,
                        "image_url": updated.image_url,
                        "edited_at": (updated.edited_at.isoformat() if getattr(updated, "edited_at", None) else None),
                    }

                await realtime_channels.broadcast(key, {"type": "message_edit", "message": payload})
                await _broadcast_mention_counts(mention_affected)
                continue

            if event_type != "message":
                continue

            content = (data.get("content") or "").strip()
            image_url = (data.get("image_url") or "").strip()
            client_nonce = str(data.get("client_nonce") or "").strip()[:64] or None
            reply_to_id = _parse_optional_int(data.get("reply_to_id"))
            if not content and not image_url:
                await realtime_channels.clear_typing(key, account_id)
                continue
            if len(content) > 4000:
                content = content[:4000]
            if len(image_url) > 512:
                image_url = image_url[:512]

            async with AsyncSessionLocal() as db:
                if not await crud.is_server_member(db, server_id, account_id):
                    await websocket.close(code=1008)
                    return
                channel = await crud.get_server_channel(db, server_id, channel_id)
                if not channel:
                    await websocket.close(code=1008)
                    return
                requested_custom_media = bool(_MEDIA_TOKEN_RE.search(content))
                content, image_url, _media_item = await _prepare_custom_media_message(
                    db, account_id, content, image_url, context="server", server_id=server_id
                )
                if not content and not image_url:
                    await realtime_channels.clear_typing(key, account_id)
                    if requested_custom_media:
                        await websocket.send_json(
                            {"type": "message_error", "error": "media_not_allowed"}
                        )
                    continue
                retry_after_ms = await message_rate_limiter.check(account_id)
                if retry_after_ms:
                    await realtime_channels.clear_typing(key, account_id)
                    await websocket.send_json(_message_rate_limit_payload(retry_after_ms))
                    continue
                reply_id = None
                if reply_to_id:
                    reply_msg = await crud.get_server_message(db, server_id, channel_id, reply_to_id)
                    if reply_msg:
                        reply_id = reply_msg.id
                msg = await crud.create_server_message(db, server_id, channel_id, account_id, content, image_url, reply_to_id=reply_id)
                mention_affected = await _sync_server_message_mentions(db, msg)
                realtime_event = await _server_message_realtime_event(
                    db, msg, client_nonce=client_nonce
                )

            await realtime_channels.clear_typing(key, account_id)
            await realtime_channels.broadcast(key, realtime_event)
            await _broadcast_mention_counts(mention_affected)

    except WebSocketDisconnect:
        pass
    except Exception:
        # Keep the app alive even if one socket sends broken data.
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await realtime_channels.disconnect(key, account_id, websocket, profile)


# --- Direct messages ---------------------------------------------------------

@router.get("/dm/{username}")
async def dm_chat_view(username: str, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    await crud.touch_last_seen(db, account.id)
    await crud.ensure_default_channels(db)

    other = await crud.get_account_by_username(db, username)
    if not other or other.id == account.id:
        return RedirectResponse(url="/community", status_code=303)

    thread = await crud.get_or_create_dm_thread(db, account.id, other.id)
    if not thread:
        return RedirectResponse(url="/community", status_code=303)

    channels = await crud.list_channels(db)
    online_members = await crud.list_online_accounts(db)
    online_ids = [m.id for m in online_members]
    friends = await crud.list_friends(db, account.id)
    dm_threads = await crud.list_dm_threads_for_account(db, account.id)
    messages = await crud.list_dm_messages(db, thread.id)
    mentions_were_read = await crud.mark_dm_mentions_read(db, account.id, thread.id)
    rail = await server_rail_context(db, account.id)
    if mentions_were_read:
        await _broadcast_mention_counts([account.id])

    return templates.TemplateResponse(
        "dm_chat.html",
        {
            "request": request,
            "account": account,
            "other": other,
            "thread": thread,
            "messages": messages,
            "online_members": online_members,
            "online_ids": online_ids,
            "channels": channels,
            "friends": friends,
            "dm_threads": dm_threads,
            **rail,
        },
    )


@router.post("/dm/{username}/message")
async def dm_message_submit(
    username: str,
    request: Request,
    content: str = Form(""),
    image_url: str = Form(""),
    reply_to_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    other = await crud.get_account_by_username(db, username)
    if not other or other.id == account.id:
        return RedirectResponse(url="/community", status_code=303)

    thread = await crud.get_or_create_dm_thread(db, account.id, other.id)
    content, image_url, _media_item = await _prepare_custom_media_message(
        db, account.id, content, image_url, context="dm", server_id=None
    )
    realtime_payload = None
    if thread and (content.strip() or image_url.strip()):
        retry_after_ms = await message_rate_limiter.check(account.id)
        if retry_after_ms:
            return _message_rate_limit_redirect(
                f"/community/dm/{other.username}", retry_after_ms
            )
        reply_id = _parse_optional_int(reply_to_id)
        if reply_id:
            reply_msg = await crud.get_dm_message(db, thread.id, reply_id)
            if not reply_msg:
                reply_id = None
        msg = await crud.create_dm_message(db, thread.id, account.id, content.strip(), image_url.strip(), reply_to_id=reply_id)
        mention_affected = await _sync_dm_message_mentions(db, msg)
        realtime_payload = await _dm_message_realtime_event(db, msg)
        await _broadcast_mention_counts(mention_affected)
        await _emit_dm_sidebar_update(int(thread.id), int(msg.id))
    if realtime_payload:
        await realtime_channels.broadcast((0, int(thread.id)), realtime_payload)
    return RedirectResponse(url=f"/community/dm/{other.username}", status_code=303)


@router.post("/dm/{username}/message/{message_id}/edit")
async def dm_message_edit_submit(
    username: str,
    message_id: int,
    request: Request,
    content: str = Form(""),
    image_url: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    other = await crud.get_account_by_username(db, username)
    if not other:
        return RedirectResponse(url="/community", status_code=303)
    thread = await crud.get_dm_thread_between(db, account.id, other.id)
    if not thread:
        return RedirectResponse(url=f"/community/dm/{other.username}", status_code=303)
    message = await crud.get_dm_message(db, thread.id, message_id)
    if (
        message
        and message.author_id == account.id
        and not getattr(message, "is_forwarded", False)
        and not crud.parse_nitro_dm_gift_marker(message.content)
        and content.strip()
    ):
        updated = await crud.update_dm_message(db, message, content.strip(), image_url.strip())
        mention_affected = await _sync_dm_message_mentions(db, updated)
        await _broadcast_mention_counts(mention_affected)
        await realtime_channels.broadcast(
            (0, thread.id),
            {
                "type": "message_edit",
                "message": {
                    "id": updated.id,
                    "content": updated.content,
                    "image_url": updated.image_url,
                    "edited_at": (updated.edited_at.isoformat() if getattr(updated, "edited_at", None) else None),
                },
            },
        )
    return RedirectResponse(url=f"/community/dm/{other.username}#dm-message-{message_id}", status_code=303)


@router.post("/dm/{username}/message/{message_id}/delete")
async def dm_message_delete_submit(
    username: str,
    message_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    other = await crud.get_account_by_username(db, username)
    if not other:
        return RedirectResponse(url="/community", status_code=303)
    thread = await crud.get_dm_thread_between(db, account.id, other.id)
    if thread:
        message = await crud.get_dm_message(db, thread.id, message_id)
        if message and message.author_id == account.id and not crud.parse_nitro_dm_gift_marker(message.content):
            mention_affected = await crud.delete_message_mentions(db, "dm", message.id)
            await crud.delete_dm_message(db, message)
            await _broadcast_mention_counts(mention_affected)
            await realtime_channels.broadcast(
                (0, int(thread.id)),
                {"type": "message_delete", "message_id": int(message_id)},
            )
    return RedirectResponse(url=f"/community/dm/{other.username}", status_code=303)



@router.websocket("/ws/dm/{thread_id}")
async def ws_dm_thread(websocket: WebSocket, thread_id: int):
    """Realtime direct messages + typing indicator.

    Optimized for a free PostgreSQL tier:
    - typing is RAM-only and expires automatically;
    - there is no polling loop;
    - DB is touched only when a real message is sent.
    """
    account_id = _ws_account_id(websocket)
    if not account_id:
        await websocket.close(code=1008)
        return

    async with AsyncSessionLocal() as db:
        account = await crud.get_account_by_id(db, account_id)
        thread = await crud.get_dm_thread_by_id(db, thread_id)
        if not account or not thread or account_id not in {thread.user_low_id, thread.user_high_id}:
            await websocket.close(code=1008)
            return
        profile = _account_payload(account)

    # Reuse the same lightweight manager. key (0, thread_id) cannot collide with real server_id.
    key = (0, thread_id)
    await realtime_channels.connect(key, account_id, websocket, profile)

    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            event_type = str(data.get("type") or "").strip().lower()

            if event_type in {"leave", "disconnect", "close"}:
                break

            if event_type == "sync":
                try:
                    after_id = max(0, int(data.get("after_id") or 0))
                except Exception:
                    after_id = 0
                async with AsyncSessionLocal() as db:
                    if not await crud.is_dm_participant(db, thread_id, account_id):
                        await websocket.close(code=1008)
                        return
                    missed = await crud.list_dm_messages_after(
                        db, thread_id, after_id, limit=201
                    )
                    has_more = len(missed) > 200
                    events = [
                        await _dm_message_realtime_event(db, message)
                        for message in missed[:200]
                    ]
                await websocket.send_json(
                    {"type": "message_sync", "events": events, "has_more": has_more}
                )
                continue

            if event_type == "typing":
                await realtime_channels.set_typing(key, account_id, profile)
                continue

            if event_type == "typing_stop":
                await realtime_channels.clear_typing(key, account_id)
                continue

            if event_type == "edit":
                try:
                    edit_id = int(data.get("id") or data.get("message_id") or 0)
                except Exception:
                    edit_id = 0
                content = (data.get("content") or "").strip()
                image_url = (data.get("image_url") or "").strip()
                if not edit_id or not content:
                    continue
                if len(content) > 4000:
                    content = content[:4000]
                if len(image_url) > 512:
                    image_url = image_url[:512]

                async with AsyncSessionLocal() as db:
                    if not await crud.is_dm_participant(db, thread_id, account_id):
                        await websocket.close(code=1008)
                        return
                    message = await crud.get_dm_message(db, thread_id, edit_id)
                    if (
                        not message
                        or message.author_id != account_id
                        or getattr(message, "is_forwarded", False)
                        or crud.parse_nitro_dm_gift_marker(message.content)
                    ):
                        continue
                    updated = await crud.update_dm_message(db, message, content, image_url)
                    mention_affected = await _sync_dm_message_mentions(db, updated)
                    payload = {
                        "id": updated.id,
                        "content": updated.content,
                        "image_url": updated.image_url,
                        "edited_at": (updated.edited_at.isoformat() if getattr(updated, "edited_at", None) else None),
                    }

                await realtime_channels.broadcast(key, {"type": "message_edit", "message": payload})
                await _broadcast_mention_counts(mention_affected)
                continue

            if event_type != "message":
                continue

            content = (data.get("content") or "").strip()
            image_url = (data.get("image_url") or "").strip()
            client_nonce = str(data.get("client_nonce") or "").strip()[:64] or None
            reply_to_id = _parse_optional_int(data.get("reply_to_id"))
            if not content and not image_url:
                await realtime_channels.clear_typing(key, account_id)
                continue
            if len(content) > 4000:
                content = content[:4000]
            if len(image_url) > 512:
                image_url = image_url[:512]

            async with AsyncSessionLocal() as db:
                if not await crud.is_dm_participant(db, thread_id, account_id):
                    await websocket.close(code=1008)
                    return
                requested_custom_media = bool(_MEDIA_TOKEN_RE.search(content))
                content, image_url, _media_item = await _prepare_custom_media_message(
                    db, account_id, content, image_url, context="dm", server_id=None
                )
                if not content and not image_url:
                    await realtime_channels.clear_typing(key, account_id)
                    if requested_custom_media:
                        await websocket.send_json(
                            {"type": "message_error", "error": "media_not_allowed"}
                        )
                    continue
                retry_after_ms = await message_rate_limiter.check(account_id)
                if retry_after_ms:
                    await realtime_channels.clear_typing(key, account_id)
                    await websocket.send_json(_message_rate_limit_payload(retry_after_ms))
                    continue
                reply_id = None
                if reply_to_id:
                    reply_msg = await crud.get_dm_message(db, thread_id, reply_to_id)
                    if reply_msg:
                        reply_id = reply_msg.id
                msg = await crud.create_dm_message(db, thread_id, account_id, content, image_url, reply_to_id=reply_id)
                mention_affected = await _sync_dm_message_mentions(db, msg)
                realtime_event = await _dm_message_realtime_event(
                    db, msg, client_nonce=client_nonce
                )

            await realtime_channels.clear_typing(key, account_id)
            await realtime_channels.broadcast(key, realtime_event)
            await _broadcast_mention_counts(mention_affected)
            await _emit_dm_sidebar_update(thread_id, int(msg.id))

    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await realtime_channels.disconnect(key, account_id, websocket, profile)


@router.post("/presence/status")
async def presence_status_update(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return JSONResponse({"ok": False, "error": "not_authenticated"}, status_code=401)

    status_value = "online"
    try:
        data = await request.json()
        if isinstance(data, dict):
            status_value = data.get("status") or status_value
    except Exception:
        form = await request.form()
        status_value = form.get("status") or status_value

    updated = await crud.update_presence_status(db, account.id, str(status_value))
    final_status = updated.account_status if updated else "online"
    updated_profile = _account_payload(updated or account)
    await account_realtime.set_profile_and_broadcast(updated_profile)
    presence_payload = {
        "type": "presence",
        "account_id": int(account.id),
        "online": final_status != "invisible",
        "status": final_status if final_status != "invisible" else "offline",
        "username": account.username,
    }
    # Keep legacy channel sockets informed while all new pages also receive the
    # global account-socket event.
    await realtime_channels.broadcast_presence_everywhere(presence_payload)
    return JSONResponse({"ok": True, "status": final_status})


# --- Public profiles ---------------------------------------------------------

@router.get("/profile/{username}")
async def public_profile(username: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    profile_account = await crud.get_account_by_username(db, username)

    if not profile_account:
        return templates.TemplateResponse(
            "profile_not_found.html", {"request": request}, status_code=404
        )

    friends = await crud.list_friends(db, profile_account.id)
    gifts = await crud.list_gifts_for_account(db, profile_account.id)
    profile_nitro = await crud.nitro_profile_payload(db, profile_account.id)

    return templates.TemplateResponse(
        "public_profile.html",
        {
            "request": request,
            "profile": profile_account,
            "viewer": viewer,
            "is_own": bool(viewer and viewer.id == profile_account.id),
            "friends": friends,
            "gifts": gifts,
            "nitro": profile_nitro,
        },
    )


@router.get("/settings")
async def settings_form(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    return templates.TemplateResponse("settings.html", {"request": request, "account": account})


@router.post("/settings")
async def settings_submit(
    request: Request,
    avatar_url: str = Form(""),
    banner_url: str = Form(""),
    bio: str = Form(""),
    next_url: str = Form(""),
    avatar_file: UploadFile | None = File(None),
    banner_file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    avatar_final = await _profile_image_url_from_form(avatar_file, avatar_url, account.id, "avatar")
    banner_final = await _profile_image_url_from_form(banner_file, banner_url, account.id, "banner")

    await crud.update_own_profile(
        db, account.id,
        avatar_url=avatar_final,
        banner_url=banner_final,
        bio=bio.strip(),
    )
    target = _safe_next_url(next_url, "/community")
    return RedirectResponse(url=target, status_code=303)


# --- Friends (AJAX endpoints -- called from public_profile.html / home.html) ---

@router.post("/api/friends/request/{username}")
async def api_send_friend_request(username: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)

    target = await crud.get_account_by_username(db, username)
    if not target:
        return JSONResponse({"error": "not_found"}, status_code=404)

    await crud.send_friend_request(db, viewer.id, target.id)
    status = await crud.friendship_status(db, viewer.id, target.id)
    return JSONResponse({"status": status})


@router.post("/api/friends/respond/{friendship_id}")
async def api_respond_friend_request(friendship_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)

    body = await request.json()
    accept = bool(body.get("accept"))

    friendship = await crud.respond_friend_request(db, friendship_id, viewer.id, accept)
    if not friendship:
        return JSONResponse({"error": "not_found"}, status_code=404)

    return JSONResponse({"status": friendship.status})


@router.get("/api/friend-status/{username}")
async def api_friend_status(username: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    target = await crud.get_account_by_username(db, username)
    if not viewer or not target:
        return JSONResponse({"status": "none"})

    friendship = await crud.get_friendship_between(db, viewer.id, target.id)
    status = await crud.friendship_status(db, viewer.id, target.id)
    return JSONResponse({"status": status, "friendship_id": friendship.id if friendship else None})


@router.post("/api/friends/remove/{username}")
async def api_remove_friend(username: str, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)

    target = await crud.get_account_by_username(db, username)
    if not target:
        return JSONResponse({"error": "not_found"}, status_code=404)

    await crud.remove_friendship(db, viewer.id, target.id)
    status = await crud.friendship_status(db, viewer.id, target.id)
    return JSONResponse({"status": status})



@router.post("/api/friends/cancel/{friendship_id}")
async def api_cancel_friend_request(friendship_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ok = await crud.cancel_pending_friend_request(db, friendship_id, viewer.id)
    if not ok:
        return JSONResponse({"error": "request not found"}, status_code=404)
    return JSONResponse({"status": "cancelled"})


@router.get("/api/notifications")
async def api_notifications(request: Request, db: AsyncSession = Depends(get_db)):
    """Polled by the bell icon. Includes friend requests + server invites."""
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"count": 0, "items": []})

    friend_pending = await crud.list_pending_requests_with_requester(db, viewer.id)
    server_pending = await crud.list_pending_server_invites(db, viewer.id)

    items = []
    for p in friend_pending:
        requester = p["requester"]
        items.append({
            "type": "friend_request",
            "friendship_id": p["friendship_id"],
            "username": requester.username if requester else "?",
            "avatar_url": requester.avatar_url if requester else None,
        })

    for p in server_pending:
        invite = p["invite"]
        server = p["server"]
        inviter = p["inviter"]
        items.append({
            "type": "server_invite",
            "invite_id": str(getattr(invite, "code", None) or invite.id),
            "server_id": invite.server_id,
            "server_name": server.name if server else "Сервер",
            "icon_url": server.icon_url if server else None,
            "inviter_username": inviter.username if inviter else "?",
        })

    return JSONResponse({"count": len(items), "items": items})
