# Telegram Bot Admin Dashboard — Backend

FastAPI + SQLAlchemy (async) + PostgreSQL (Render) backend that renders an
admin dashboard at `/admin`.

## Project structure

```
telegram_admin_backend/
├── app/
│   ├── __init__.py
│   ├── config.py      # env var / settings loading (pydantic-settings)
│   ├── database.py    # async engine, session factory, init_db()
│   ├── models.py      # User, Log ORM models
│   ├── crud.py         # analytics queries
│   └── main.py         # FastAPI app + /admin route
├── templates/
│   └── dashboard.html  # replace with your real template if different
├── requirements.txt
├── .env.example
└── README.md
```

## 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure the database URL

Copy `.env.example` to `.env` and paste in the connection string from your
Render Postgres instance's dashboard ("External Database URL" if you're
running the app outside Render, "Internal Database URL" if the app is also
deployed on Render):

```bash
cp .env.example .env
```

```
DATABASE_URL=postgres://user:password@dpg-xxxxxxxx-a.oregon-postgres.render.com/dbname
AUTO_CREATE_TABLES=true
```

You can paste Render's URL exactly as given (`postgres://...`) — the app
automatically rewrites it to `postgresql+asyncpg://...` internally so
SQLAlchemy's async engine can use it.

## 3. Initialize the database tables

You don't need a separate migration step for a first run: with
`AUTO_CREATE_TABLES=true`, the app calls `init_db()` on startup, which runs
the equivalent of:

```sql
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    user_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS logs (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    username VARCHAR(255),
    action VARCHAR(255) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

If you'd rather manage schema changes explicitly (recommended once you're in
production), set `AUTO_CREATE_TABLES=false` and use a tool like Alembic
instead, or run the SQL above manually once against your Render database.

## 4. Run the app locally

```bash
uvicorn app.main:app --reload
```

Then open:

- Dashboard: http://127.0.0.1:8000/admin
- Health check: http://127.0.0.1:8000/health

## 5. Deploying on Render

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Add `DATABASE_URL` (and optionally `AUTO_CREATE_TABLES`) as environment
  variables in the Render service settings — use the **Internal Database
  URL** of your Postgres instance since both services live on Render.

## Notes

- `templates/dashboard.html` here is a minimal placeholder that already
  matches the variable names the route passes in (`total_users`,
  `active_users_24h`, `mini_app_opens`, `recent_logs`). Swap in your actual
  designed template — just keep those variable/field names, or update
  `app/main.py` to match your template's names.
- Each `Log` object passed to the template exposes `.timestamp`, `.user_id`,
  `.username`, and `.action` attributes directly (it's the SQLAlchemy ORM
  object), so `{{ log.action }}` etc. works as-is in Jinja2.
