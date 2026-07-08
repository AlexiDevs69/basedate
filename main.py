"""
FastAPI application entry point.

Run locally with:
    uvicorn main:app --reload

On Render, the Start Command is:
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

import crud
import lastfm
from community import crud as community_crud
from community.router import router as community_router
from config import get_settings
from database import get_db, init_db
from telegram import send_telegram_message

settings = get_settings()

app = FastAPI(title="Telegram Bot Admin Dashboard")

# Signs the session cookie so it can't be tampered with client-side.
# SECRET_KEY must be set to a long random string in production.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Everything under /community/* (registration, login, public profiles,
# online members) lives in its own module -- see community/router.py.
app.include_router(community_router)


@app.on_event("startup")
async def on_startup() -> None:
    if settings.auto_create_tables:
        await init_db()


def is_logged_in(request: Request) -> bool:
    """Checks the signed session cookie for a valid login flag."""
    return bool(request.session.get("logged_in"))


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
    """Validates credentials against ADMIN_USERNAME / ADMIN_PASSWORD env vars."""
    if username == settings.admin_username and password == settings.admin_password:
        request.session["logged_in"] = True
        request.session["username"] = username
        return RedirectResponse(url="/admin", status_code=303)

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid username or password."},
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request):
    """Clears the session and sends the visitor back to the login page."""
    request.session.clear()
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

    return templates.TemplateResponse("community_user_edit.html", {"request": request, "account": account})


@app.post("/community-users/{account_id}/edit")
async def community_user_edit_submit(
    account_id: int,
    request: Request,
    role_label: str = Form(""),
    role_color_start: str = Form(""),
    role_color_end: str = Form(""),
    is_verified: str | None = Form(None),
    is_banned: str | None = Form(None),
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
    )
    return RedirectResponse(url="/community-users", status_code=303)


@app.post("/community-users/{account_id}/delete")
async def community_user_delete(account_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)

    await community_crud.delete_account(db, account_id)
    return RedirectResponse(url="/community-users", status_code=303)


@app.get("/health")
async def health_check():
    """Simple liveness endpoint -- intentionally not behind login."""
    return {"status": "ok"}
