"""
Routes for the public-facing community module: registration, login
(email/password + Telegram Login Widget), logout, the main "who's online"
page, public profile pages, and self-service profile editing.

Mounted into the main app via `app.include_router(community_router)` in
main.py -- everything here lives under the /community prefix so it can
never collide with the admin dashboard's routes.
"""
import asyncio
import hashlib
import os
import re
import time
import uuid
from pathlib import Path

import httpx

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from community import auth, crud
from config import get_settings
from database import AsyncSessionLocal, get_db

settings = get_settings()
router = APIRouter(prefix="/community", tags=["community"])

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ROOT_DIR = Path(__file__).resolve().parents[1]
PROFILE_UPLOAD_DIR = ROOT_DIR / "static" / "uploads" / "profiles"
ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
MAX_PROFILE_IMAGE_BYTES = 5 * 1024 * 1024


def _safe_next_url(next_url: str | None, fallback: str = "/community") -> str:
    target = (next_url or "").strip()
    if target.startswith("/community") and not target.startswith("//"):
        return target
    return fallback


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


# --- Lightweight realtime layer --------------------------------------------
# This is intentionally in-memory: typing state is NOT written to PostgreSQL.
# DB is touched only when a real message is created/edited/deleted.
class RealtimeChannelManager:
    def __init__(self) -> None:
        self.connections: dict[tuple[int, int], dict[int, set[WebSocket]]] = {}
        self.typing: dict[tuple[int, int], dict[int, dict]] = {}
        self.lock = asyncio.Lock()

    async def connect(self, key: tuple[int, int], account_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self.lock:
            self.connections.setdefault(key, {}).setdefault(account_id, set()).add(websocket)

    async def disconnect(self, key: tuple[int, int], account_id: int, websocket: WebSocket) -> None:
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
                for account_sockets in users.values():
                    for ws in dead:
                        account_sockets.discard(ws)

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
    }



async def current_account(request: Request, db: AsyncSession):
    """Returns the logged-in Account for this visitor, or None."""
    account_id = auth.get_logged_in_account_id(request)
    if not account_id:
        return None
    return await crud.get_account_by_id(db, account_id)


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
        {"request": request, "account": account, "error": None, **rail},
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
            {"request": request, "account": account, "error": "Назва сервера мінімум 2 символи.", **rail},
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


@router.get("/servers/{server_id}")
async def server_home(server_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return RedirectResponse(url="/community", status_code=303)

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
        return RedirectResponse(url="/community", status_code=303)

    await crud.touch_last_seen(db, account.id)
    server = await crud.get_server_by_id(db, server_id)
    channel = await crud.get_server_channel(db, server_id, channel_id)
    if not server or not channel:
        return RedirectResponse(url=f"/community/servers/{server_id}", status_code=303)

    channels = await crud.list_server_channels(db, server_id)
    members = await crud.list_server_members(db, server_id)
    friends = await crud.list_friends(db, account.id)
    feed = await crud.get_server_feed(db, server_id, channel_id)
    rail = await server_rail_context(db, account.id, active_server_id=server_id)
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


@router.post("/servers/{server_id}/channel/{channel_id}/message")
async def server_message_submit(
    server_id: int,
    channel_id: int,
    request: Request,
    content: str = Form(""),
    image_url: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return RedirectResponse(url="/community", status_code=303)

    channel = await crud.get_server_channel(db, server_id, channel_id)
    if channel and (content.strip() or image_url.strip()):
        await crud.create_server_message(db, server_id, channel_id, account.id, content.strip(), image_url.strip())
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
        return RedirectResponse(url="/community", status_code=303)

    message = await crud.get_server_message(db, server_id, channel_id, message_id)
    if message and message.author_id == account.id and content.strip():
        await crud.update_server_message(db, message, content.strip(), image_url.strip())
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
        return RedirectResponse(url="/community", status_code=303)

    message = await crud.get_server_message(db, server_id, channel_id, message_id)
    if message and (message.author_id == account.id or await crud.can_manage_server(db, server_id, account.id)):
        await crud.delete_server_message(db, message)
    return RedirectResponse(url=f"/community/servers/{server_id}/channel/{channel_id}", status_code=303)


@router.post("/servers/{server_id}/settings")
async def server_settings_submit(
    server_id: int,
    request: Request,
    name: str = Form(...),
    icon_url: str = Form(""),
    description: str = Form(""),
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
    redirect_to: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)
    if not await crud.is_server_member(db, server_id, account.id):
        return RedirectResponse(url="/community", status_code=303)

    target = await crud.get_account_by_username(db, username.strip())
    if target:
        await crud.invite_friend_to_server(db, server_id, account.id, target.id)
    safe_redirect = redirect_to if redirect_to.startswith("/community/") else f"/community/servers/{server_id}"
    return RedirectResponse(url=safe_redirect, status_code=303)


@router.post("/api/server-invites/respond/{invite_id}")
async def api_respond_server_invite(invite_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)

    body = await request.json()
    accept = bool(body.get("accept"))
    invite = await crud.respond_server_invite(db, invite_id, viewer.id, accept)
    if not invite:
        return JSONResponse({"error": "not_found"}, status_code=404)

    return JSONResponse({"status": invite.status, "server_id": invite.server_id})




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
    await realtime_channels.connect(key, account_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            if event_type == "typing":
                await realtime_channels.set_typing(key, account_id, profile)
                continue

            if event_type == "typing_stop":
                await realtime_channels.clear_typing(key, account_id)
                continue

            if event_type != "message":
                continue

            content = (data.get("content") or "").strip()
            image_url = (data.get("image_url") or "").strip()
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
                msg = await crud.create_server_message(db, server_id, channel_id, account_id, content, image_url)
                created_at = msg.created_at.isoformat()

            await realtime_channels.clear_typing(key, account_id)
            await realtime_channels.broadcast(
                key,
                {
                    "type": "message",
                    "message": {
                        "id": msg.id,
                        "server_id": server_id,
                        "channel_id": channel_id,
                        "author_id": account_id,
                        "content": content,
                        "image_url": image_url or None,
                        "created_at": created_at,
                    },
                    "author": profile,
                },
            )

    except WebSocketDisconnect:
        pass
    except Exception:
        # Keep the app alive even if one socket sends broken data.
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await realtime_channels.disconnect(key, account_id, websocket)


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
    rail = await server_rail_context(db, account.id)

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
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    other = await crud.get_account_by_username(db, username)
    if not other or other.id == account.id:
        return RedirectResponse(url="/community", status_code=303)

    thread = await crud.get_or_create_dm_thread(db, account.id, other.id)
    if thread and content.strip():
        await crud.create_dm_message(db, thread.id, account.id, content.strip(), image_url.strip())
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
    if message and message.author_id == account.id and content.strip():
        await crud.update_dm_message(db, message, content.strip(), image_url.strip())
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
        if message and message.author_id == account.id:
            await crud.delete_dm_message(db, message)
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
    await realtime_channels.connect(key, account_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            if event_type == "typing":
                await realtime_channels.set_typing(key, account_id, profile)
                continue

            if event_type == "typing_stop":
                await realtime_channels.clear_typing(key, account_id)
                continue

            if event_type != "message":
                continue

            content = (data.get("content") or "").strip()
            image_url = (data.get("image_url") or "").strip()
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
                msg = await crud.create_dm_message(db, thread_id, account_id, content, image_url)
                created_at = msg.created_at.isoformat()

            await realtime_channels.clear_typing(key, account_id)
            await realtime_channels.broadcast(
                key,
                {
                    "type": "message",
                    "message": {
                        "id": msg.id,
                        "thread_id": thread_id,
                        "author_id": account_id,
                        "content": content,
                        "image_url": image_url or None,
                        "created_at": created_at,
                    },
                    "author": profile,
                },
            )

    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await realtime_channels.disconnect(key, account_id, websocket)


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
    return JSONResponse({"ok": True, "status": (updated.account_status if updated else "online")})


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

    return templates.TemplateResponse(
        "public_profile.html",
        {
            "request": request,
            "profile": profile_account,
            "viewer": viewer,
            "is_own": bool(viewer and viewer.id == profile_account.id),
            "friends": friends,
            "gifts": gifts,
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
            "invite_id": invite.id,
            "server_id": invite.server_id,
            "server_name": server.name if server else "Сервер",
            "icon_url": server.icon_url if server else None,
            "inviter_username": inviter.username if inviter else "?",
        })

    return JSONResponse({"count": len(items), "items": items})
