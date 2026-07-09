"""
Routes for the public-facing community module: registration, login
(email/password + Telegram Login Widget), logout, the main "who's online"
page, public profile pages, and self-service profile editing.

Mounted into the main app via `app.include_router(community_router)` in
main.py -- everything here lives under the /community prefix so it can
never collide with the admin dashboard's routes.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from community import auth, crud
from config import get_settings
from database import get_db

settings = get_settings()
router = APIRouter(prefix="/community", tags=["community"])

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def current_account(request: Request, db: AsyncSession):
    """Returns the logged-in Account for this visitor, or None."""
    account_id = auth.get_logged_in_account_id(request)
    if not account_id:
        return None
    return await crud.get_account_by_id(db, account_id)


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
async def community_home(request: Request, db: AsyncSession = Depends(get_db)):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    await crud.touch_last_seen(db, account.id)
    await crud.ensure_default_channels(db)
    channels = await crud.list_channels(db)
    online_members = await crud.list_online_accounts(db)

    return templates.TemplateResponse(
        "home.html",
        {"request": request, "account": account, "online_members": online_members, "channels": channels},
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

    return templates.TemplateResponse(
        "channel.html",
        {"request": request, "account": account, "channels": channels, "channel": channel, "feed": feed},
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
    db: AsyncSession = Depends(get_db),
):
    account = await current_account(request, db)
    if not account:
        return RedirectResponse(url="/community/login", status_code=303)

    await crud.update_own_profile(
        db, account.id,
        avatar_url=avatar_url.strip(), banner_url=banner_url.strip(), bio=bio.strip(),
    )
    return RedirectResponse(url=f"/community/profile/{account.username}", status_code=303)


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


@router.get("/api/notifications")
async def api_notifications(request: Request, db: AsyncSession = Depends(get_db)):
    """Polled every ~20s by the bell icon on home.html / public_profile.html."""
    viewer = await current_account(request, db)
    if not viewer:
        return JSONResponse({"count": 0, "items": []})

    pending = await crud.list_pending_requests_with_requester(db, viewer.id)
    return JSONResponse({
        "count": len(pending),
        "items": [
            {
                "friendship_id": p["friendship_id"],
                "username": p["requester"].username if p["requester"] else "?",
                "avatar_url": p["requester"].avatar_url if p["requester"] else None,
            }
            for p in pending
        ],
    })
