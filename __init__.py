# Telegram Bot Admin Dashboard — Backend (flat layout)

Matches your `AlexiDevs69/basedate` repo structure: all Python files sit in
the repo root (no `app/` package folder).

## Files

```
basedate/
├── main.py          # FastAPI app + /admin route
├── config.py        # loads DATABASE_URL etc. from environment
├── database.py      # async SQLAlchemy engine/session + init_db()
├── models.py        # User, Log ORM models
├── crud.py          # analytics queries used by /admin
├── templates/
│   └── index.html   # the dashboard template
├── requirements.txt
├── runtime.txt       # pins Python to 3.12.7
└── .env.example
```

## Render setup

**Build Command**
```
pip install -r requirements.txt
```

**Start Command**
```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

**Environment Variables** (Web Service → Environment tab)
- `DATABASE_URL` — the Internal Database URL from your Render Postgres instance
- `AUTO_CREATE_TABLES` — `true` (creates `users`/`logs` tables automatically on first boot)
- `PYTHON_VERSION` — `3.12.7` (belt-and-suspenders in case `runtime.txt` gets ignored)

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # then edit DATABASE_URL inside it
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/admin

## Notes

- All imports between these files are "flat" (e.g. `from models import Log, User`,
  not `from app.models import ...`) to match this repo's structure — don't
  re-add an `app.` prefix anywhere.
- `templates/index.html` is a placeholder that already matches the variable
  names the route passes in (`total_users`, `active_users_24h`,
  `mini_app_opens`, `recent_logs`). Replace it with your real design,
  keeping those variable names (or edit `main.py`'s `TemplateResponse` call
  to match your template).
