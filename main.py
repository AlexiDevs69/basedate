"""
FastAPI application entry point.

Run locally with:
    uvicorn main:app --reload

On Render, the Start Command is:
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import hmac
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

import crud
import lastfm
from community import auth as community_auth
from community import crud as community_crud
from community.router import router as community_router
from config import get_settings
from database import get_db, init_db
from telegram import send_telegram_message

settings = get_settings()
logger = logging.getLogger("admin")

_DEFAULT_SECRET_KEY = "insecure-dev-secret-change-me"
_DEFAULT_ADMIN_PASSWORD = "changeme"

app = FastAPI(title="Telegram Bot Admin Dashboard")

# Дозволяє окремо захостованій gateway-сторінці (wake-up preloader) читати
# відповідь /api/ping через fetch. Домени задаються змінною GATEWAY_ORIGINS
# (через кому). Якщо вона порожня -- крос-доменний доступ вимкнений.
# "*" тут НЕ підходить, бо на цьому ж app ходять сесійні cookie.
_gateway_origins = [o.strip() for o in settings.gateway_origins.split(",") if o.strip()]
if _gateway_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_gateway_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

# Signs the session cookie so it can't be tampered with client-side.
# If SECRET_KEY is missing / still the public default anyone could forge a
# "logged in" cookie, so in that case use a random per-process key instead
# (everyone is simply logged out after a restart) and complain loudly.
_session_secret = settings.secret_key
if not _session_secret or _session_secret == _DEFAULT_SECRET_KEY:
    _session_secret = secrets.token_hex(32)
    print(
        "[security] SECRET_KEY is not set (or is the public default). Using a random "
        "temporary key: sessions reset on every restart. Set SECRET_KEY to a long random "
        "string (openssl rand -hex 32).",
        flush=True,
    )

_https_only = (
    settings.session_https_only
    if settings.session_https_only is not None
    else bool(os.getenv("RENDER"))
)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=_https_only,
)

# Admin POST endpoints live outside /community (which has its own origin
# guard). Browsers always send Origin on cross-site POSTs, so rejecting a
# foreign Origin stops CSRF on the admin panel even without form tokens.
_ADMIN_POST_PREFIXES = ("/login", "/profile", "/broadcast", "/settings", "/community-users", "/gifts")


@app.middleware("http")
async def admin_same_origin_guard(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith(
        _ADMIN_POST_PREFIXES
    ):
        if not community_auth.origin_is_trusted(request):
            return PlainTextResponse("Cross-site request blocked.", status_code=403)
    return await call_next(request)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# User-uploaded profile media. On Render's free web service filesystem this is
# local/ephemeral; if IMGUR_CLIENT_ID is set, community/router.py will store
# profile images on Imgur instead and only keep the returned URL in PostgreSQL.
STATIC_DIR = BASE_DIR / "static"
(STATIC_DIR / "uploads" / "profiles").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.on_event("startup")
async def on_startup() -> None:
    if _admin_uses_default_password():
        print(
            "[security] ADMIN_PASSWORD is the default 'changeme' -- admin login is DISABLED "
            "until you set ADMIN_PASSWORD_HASH (python scripts/hash_admin_password.py) "
            "or ADMIN_PASSWORD.",
            flush=True,
        )
    if settings.auto_create_tables:
        # Render's port scanner only waits a limited time for
        # "Application startup complete." If Aiven is slow/unreachable right
        # now, an un-timed-out init_db() hangs here forever, the port never
        # opens, and the deploy fails with "No open ports detected" even
        # though nothing is actually broken -- it's just still waiting.
        # Fail fast instead, with a log line that says what happened.
        try:
            await asyncio.wait_for(init_db(), timeout=15)
        except asyncio.TimeoutError:
            print("[startup] init_db() timed out after 15s -- DB unreachable at startup", flush=True)
            raise


# Register the community router after the base-table startup handler so a new,
# empty database is initialized before community schema upgrades run.
app.include_router(community_router)


@app.get("/")
async def root_redirect():
    """
    The bare domain has no page of its own -- regular visitors land on the
    public AlexiHub app, not the admin dashboard. community_home() (in
    community/router.py) takes care of sending logged-out visitors on to
    /community/login from there.
    """
    return RedirectResponse(url="/community", status_code=307)


def is_logged_in(request: Request) -> bool:
    """
    Checks the signed session cookie for a valid, non-expired login.
    The login time is stored server-signed in the cookie, so a stolen cookie
    stops working after ADMIN_SESSION_HOURS even if the browser keeps it.
    """
    if not request.session.get("logged_in"):
        return False
    logged_in_at = request.session.get("logged_in_at")
    max_age = max(settings.admin_session_hours, 1) * 3600
    if not isinstance(logged_in_at, (int, float)) or time.time() - logged_in_at > max_age:
        request.session.pop("logged_in", None)
        request.session.pop("logged_in_at", None)
        return False
    return True


def _admin_credentials_ok(username: str, password: str) -> bool:
    """Constant-time check; uses ADMIN_PASSWORD_HASH (bcrypt) when configured."""
    user_ok = hmac.compare_digest(
        username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    if settings.admin_password_hash:
        pass_ok = community_auth.verify_password(password, settings.admin_password_hash)
    else:
        pass_ok = hmac.compare_digest(
            password.encode("utf-8"), settings.admin_password.encode("utf-8")
        )
    return user_ok and pass_ok


def _admin_uses_default_password() -> bool:
    return (
        not settings.admin_password_hash
        and settings.admin_password == _DEFAULT_ADMIN_PASSWORD
        and not settings.allow_insecure_admin
    )


def require_login(request: Request):
    """
    Dependency that redirects to /login if the visitor isn't authenticated.

    FastAPI dependencies can't return a redirect directly and stop the route,
    so we raise it as an HTTPException-style short-circuit via RedirectResponse
    inside the route itself instead (see admin_dashboard below) -- this
    function is kept simple and just returns the boolean for the route to act on.
    """
    return is_logged_in(request)


@app.get("/login")
async def login_form(request: Request):
    """Shows the login page. If already logged in, skip straight to /admin."""
    if is_logged_in(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None},
    )


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Validates credentials against ADMIN_USERNAME + ADMIN_PASSWORD_HASH / ADMIN_PASSWORD."""
    if _admin_uses_default_password():
        # Refuse to open the admin panel with the public default password.
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": (
                    "ADMIN_PASSWORD is still the default. Set ADMIN_PASSWORD_HASH "
                    "(or ADMIN_PASSWORD) in the environment and restart."
                ),
            },
            status_code=403,
        )

    # Brute-force protection: failed attempts per client IP.
    limit_key = f"admin-login:{community_auth.client_ip(request)}"
    if community_auth.rate_limiter.blocked(limit_key, 10, 900):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many failed attempts. Try again in 15 minutes."},
            status_code=429,
        )

    if _admin_credentials_ok(username, password):
        community_auth.rate_limiter.reset(limit_key)
        request.session["logged_in"] = True
        request.session["logged_in_at"] = time.time()
        request.session["username"] = settings.admin_username
        return RedirectResponse(url="/admin", status_code=303)

    community_auth.rate_limiter.record(limit_key, 900)
    logger.warning("Failed admin login from %s", community_auth.client_ip(request))
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid username or password."},
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request):
    """Clears the session and sends the visitor back to the login page."""
    # Only drop the admin login; a community account logged in from the same
    # browser shares this cookie and must stay signed in.
    for key in ("logged_in", "logged_in_at", "username"):
        request.session.pop(key, None)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/admin")
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Renders the admin dashboard with live analytics pulled from Postgres.
    Requires an active login session -- otherwise redirects to /login.
    """
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    total_users = await crud.get_total_users(db)
    active_users_24h = await crud.get_active_users_24h(db)
    mini_app_opens = await crud.get_mini_app_opens(db)
    recent_logs = await crud.get_recent_logs(db, limit=10)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "total_users": total_users,
            "active_users_24h": active_users_24h,
            "mini_app_opens": mini_app_opens,
            "recent_logs": recent_logs,
        },
    )


@app.get("/users")
async def users_list(request: Request, page: int = 1, db: AsyncSession = Depends(get_db)):
    """Full, paginated list of every row in the users table."""
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    page = max(page, 1)
    users, total = await crud.get_users_page(db, page=page)
    total_pages = max((total + crud.PAGE_SIZE - 1) // crud.PAGE_SIZE, 1)

    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "users": users,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


@app.get("/logs")
async def logs_list(request: Request, page: int = 1, db: AsyncSession = Depends(get_db)):
    """Full, paginated list of every row in the logs table."""
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    page = max(page, 1)
    logs, total = await crud.get_logs_page(db, page=page)
    total_pages = max((total + crud.PAGE_SIZE - 1) // crud.PAGE_SIZE, 1)

    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "logs": logs,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


@app.get("/profile")
async def profile_view(request: Request, db: AsyncSession = Depends(get_db)):
    """Shows the admin profile: avatar/banner preview + an edit form."""
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    username = request.session.get("username", "admin")
    profile = await crud.get_profile(db, username=username)
    # No OAuth to check here -- Last.fm is "connected" simply if both
    # config values are set, since it's just a public API key + username.
    lastfm_connected = bool(settings.lastfm_api_key and settings.lastfm_username)

    return templates.TemplateResponse(
        "profile.html",
        {"request": request, "profile": profile, "lastfm_connected": lastfm_connected},
    )


@app.post("/profile")
async def profile_update(
    request: Request,
    avatar_url: str = Form(""),
    banner_url: str = Form(""),
    bio: str = Form(""),
    role_label: str = Form(""),
    role_color_start: str = Form(""),
    role_color_end: str = Form(""),
    # Unchecked checkbox isn't sent at all -- same trick as maintenance_mode.
    is_verified: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Saves the new avatar_url / banner_url / bio / role / verified flag, then redirects back to /profile."""
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    await crud.update_profile(
        db,
        avatar_url=avatar_url.strip(),
        banner_url=banner_url.strip(),
        bio=bio.strip(),
        is_verified=is_verified is not None,
        role_label=role_label.strip(),
        role_color_start=role_color_start.strip(),
        role_color_end=role_color_end.strip(),
    )
    return RedirectResponse(url="/profile", status_code=303)


@app.get("/api/now-playing")
async def api_now_playing(request: Request):
    """
    JSON endpoint the profile page polls every ~15s for the "currently
    listening" widget. Behind login like everything else here.
    """
    if not is_logged_in(request):
        return JSONResponse({"connected": False}, status_code=401)

    track = await lastfm.get_now_playing()
    return JSONResponse(track or {"connected": False})


@app.get("/broadcast")
async def broadcast_form(request: Request):
    """Показує форму розсилки з живим прев'ю повідомлення."""
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse(
        "broadcast.html",
        {
            "request": request,
            "result": None,
            "bot_token_missing": not settings.bot_token,
        },
    )


@app.post("/broadcast")
async def broadcast_send(
    request: Request,
    db: AsyncSession = Depends(get_db),
    message: str = Form(...),
    parse_mode: str = Form("none"),
    button_text: str = Form(""),
    button_url: str = Form(""),
    test_chat_id: str = Form(""),
):
    """
    Надсилає повідомлення або на один тестовий Chat ID (якщо вказаний),
    або всім юзерам з таблиці users. Виконується синхронно в межах одного
    запиту -- цілком нормально для невеликої/середньої кількості юзерів
    на безкоштовному тарифі.
    """
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    if not settings.bot_token:
        return templates.TemplateResponse(
            "broadcast.html",
            {
                "request": request,
                "result": {"error": "BOT_TOKEN не налаштований на цьому сервісі."},
                "bot_token_missing": True,
            },
        )

    resolved_parse_mode = None if parse_mode == "none" else parse_mode
    btn_text = button_text.strip() or None
    btn_url = button_url.strip() or None

    # Тестовий режим: надсилаємо лише на один Chat ID, таблицю users не чіпаємо.
    test_chat_id = test_chat_id.strip()
    if test_chat_id:
        try:
            target_ids = [int(test_chat_id)]
        except ValueError:
            target_ids = []
    else:
        target_ids = await crud.get_all_user_ids(db)

    sent, failed = 0, 0
    for chat_id in target_ids:
        ok, _err = await send_telegram_message(
            settings.bot_token, chat_id, message,
            parse_mode=resolved_parse_mode,
            button_text=btn_text, button_url=btn_url,
        )
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)  # запас проти рейт-лімітів Telegram

    # Логуємо тільки реальні розсилки, не тестові -- щоб Logs лишався змістовним.
    if not test_chat_id:
        admin_username = request.session.get("username", settings.admin_username)
        await crud.log_broadcast(db, admin_username, sent, failed, message)

    return templates.TemplateResponse(
        "broadcast.html",
        {
            "request": request,
            "result": {
                "sent": sent,
                "failed": failed,
                "was_test": bool(test_chat_id),
            },
            "bot_token_missing": False,
        },
    )


@app.get("/settings")
async def settings_view(request: Request, db: AsyncSession = Depends(get_db)):
    """Shows bot settings: welcome message + maintenance-mode toggle."""
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    bot_settings = await crud.get_settings(db)

    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "settings": bot_settings},
    )


@app.post("/settings")
async def settings_update(
    request: Request,
    welcome_message: str = Form(""),
    maintenance_message: str = Form(""),
    # An unchecked checkbox simply isn't sent in the form body at all, so
    # this has to be Optional -- its presence (any value) means "checked".
    maintenance_mode: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Saves bot settings, then redirects back to /settings."""
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    await crud.update_settings(
        db,
        welcome_message=welcome_message.strip(),
        maintenance_mode=maintenance_mode is not None,
        maintenance_message=maintenance_message.strip(),
    )
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/community-users")
async def community_users_list(request: Request, page: int = 1, db: AsyncSession = Depends(get_db)):
    """Adminka list of every registered community account -- verify/role/ban/delete from here."""
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    page = max(page, 1)
    accounts, total = await community_crud.get_accounts_page(db, page=page)
    total_pages = max((total + community_crud.PAGE_SIZE - 1) // community_crud.PAGE_SIZE, 1)

    return templates.TemplateResponse(
        "community_users.html",
        {"request": request, "accounts": accounts, "page": page, "total_pages": total_pages, "total": total},
    )


@app.get("/community-users/{account_id}/edit")
async def community_user_edit_form(account_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    account = await community_crud.get_account_by_id(db, account_id)
    if not account:
        return RedirectResponse(url="/community-users", status_code=303)

    gifts = await community_crud.list_gifts(db)
    return templates.TemplateResponse(
        "community_user_edit.html",
        {"request": request, "account": account, "gifts": gifts},
    )


@app.post("/community-users/{account_id}/edit")
async def community_user_edit_submit(
    account_id: int,
    request: Request,
    role_label: str = Form(""),
    role_color_start: str = Form(""),
    role_color_end: str = Form(""),
    is_verified: str | None = Form(None),
    is_banned: str | None = Form(None),
    # Visual identity. ``None`` = field not in the form body -> keep the
    # user's current value (see update_account_moderation).
    name_effect: str | None = Form(None),
    name_font: str | None = Form(None),
    name_color_start: str | None = Form(None),
    name_color_end: str | None = Form(None),
    profile_card_bg_url: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    await community_crud.update_account_moderation(
        db, account_id,
        is_verified=is_verified is not None,
        role_label=role_label.strip(),
        role_color_start=role_color_start.strip(),
        role_color_end=role_color_end.strip(),
        is_banned=is_banned is not None,
        name_effect=name_effect,
        name_font=name_font,
        name_color_start=name_color_start,
        name_color_end=name_color_end,
        profile_card_bg_url=profile_card_bg_url,
    )
    return RedirectResponse(url="/community-users", status_code=303)


@app.get("/gifts")
async def gifts_admin(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    gifts = await community_crud.list_gifts(db)
    return templates.TemplateResponse("gifts.html", {"request": request, "gifts": gifts, "error": None})


@app.post("/gifts/create")
async def gift_create(
    request: Request,
    name: str = Form(...),
    image_url: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    name = name.strip()
    image_url = image_url.strip()
    if not name or not image_url:
        gifts = await community_crud.list_gifts(db)
        return templates.TemplateResponse(
            "gifts.html",
            {"request": request, "gifts": gifts, "error": "Назва і URL картинки обов'язкові."},
            status_code=400,
        )

    await community_crud.create_gift(db, name=name, image_url=image_url, description=description.strip())
    return RedirectResponse(url="/gifts", status_code=303)


@app.post("/gifts/{gift_id}/delete")
async def gift_delete(gift_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    await community_crud.delete_gift(db, gift_id)
    return RedirectResponse(url="/gifts", status_code=303)


@app.post("/community-users/{account_id}/gift")
async def community_user_gift(
    account_id: int,
    request: Request,
    gift_id: int = Form(...),
    gifted_by: str = Form("Адміністрація"),
    message: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    await community_crud.give_gift_to_account(
        db,
        recipient_id=account_id,
        gift_id=gift_id,
        gifted_by=gifted_by.strip() or "Адміністрація",
        message=message.strip(),
    )
    return RedirectResponse(url=f"/community-users/{account_id}/edit", status_code=303)


@app.post("/community-users/{account_id}/delete")
async def community_user_delete(account_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    try:
        await community_crud.delete_account(db, account_id)
    except Exception:
        # delete_account() already rolled back, so nothing was removed.
        logger.exception("Failed to delete community account %s", account_id)
        accounts, total = await community_crud.get_accounts_page(db, page=1)
        total_pages = max((total + community_crud.PAGE_SIZE - 1) // community_crud.PAGE_SIZE, 1)
        return templates.TemplateResponse(
            "community_users.html",
            {
                "request": request,
                "accounts": accounts,
                "page": 1,
                "total_pages": total_pages,
                "total": total,
                "error": f"Не вдалося видалити акаунт #{account_id}. Нічого не змінено -- деталі в логах сервера.",
            },
            status_code=500,
        )
    return RedirectResponse(url="/community-users", status_code=303)


@app.get("/health")
async def health_check():
    """Simple liveness endpoint -- intentionally not behind login."""
    return {"status": "ok"}


@app.get("/api/ping")
async def api_ping():
    """
    Same contract as /health, under the path the wake-up gateway page
    expects. Kept separate (not just an alias/redirect) so it's obvious
    at a glance in logs/metrics which caller is hitting it.
    """
    return {"status": "ok"}
