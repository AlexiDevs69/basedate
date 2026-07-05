# Copy this file to ".env" and fill in your real values for LOCAL testing.
# On Render, set these in the Web Service's "Environment" tab instead --
# you don't need an actual .env file there.
#
# Paste Render's Postgres connection string exactly as given (it usually
# starts with "postgres://"). config.py automatically rewrites it to
# "postgresql+asyncpg://" so SQLAlchemy's async engine can use it.
DATABASE_URL=postgres://user:password@host:5432/dbname

# Optional: creates the users/logs tables automatically on startup.
AUTO_CREATE_TABLES=true
