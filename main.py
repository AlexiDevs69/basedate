"""
FastAPI application entry point.

Run locally with:
    uvicorn main:app --reload

On Render, the Start Command is:
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""

from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

import crud
from config import get_settings
from database import get_db, init_db

settings = get_settings()

app = FastAPI(title="Telegram Bot Admin Dashboard")

# Signs the session cookie so it can't be tampered with client-side.
# SECRET_KEY must be set to a long random string in production.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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


@app.get("/health")
async def health_check():
    """Simple liveness endpoint -- intentionally not behind login."""
    return {"status": "ok"}
