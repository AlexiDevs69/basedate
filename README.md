# Copy this file to ".env" and fill in your real values.
# This is the "External Database URL" (or "Internal" if deploying on Render itself)
# that Render gives you on your PostgreSQL instance's dashboard page.
#
# Render usually provides it in the classic "postgres://" form, e.g.:
#   postgres://user:password@dpg-xxxxxxxx-a.oregon-postgres.render.com/dbname
#
# You can paste it here EXACTLY as Render gives it to you -- app/config.py
# automatically rewrites the scheme to "postgresql+asyncpg://" so SQLAlchemy's
# async engine can use it.
DATABASE_URL=postgres://user:password@host:5432/dbname

# Optional: set to "true" to auto-create tables on startup (handy for first deploy/dev).
# In real production you would normally use Alembic migrations instead.
AUTO_CREATE_TABLES=true
