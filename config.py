"""
Application configuration.
Loads settings from environment variables (and a local .env file, if present)
using pydantic-settings. Keeping this in one place means the rest of the app
never touches os.environ directly.
"""
from functools import lru_cache
import re

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Raw connection string as provided by Render (or any Postgres host).
    database_url: str

    # If true, tables are created automatically on app startup.
    auto_create_tables: bool = True

    # --- Admin login credentials ---
    # Set these in Render's Environment tab. Do NOT hardcode real values here.
    admin_username: str = "admin"
    admin_password: str = "changeme"

    # Optional, recommended: bcrypt hash of the admin password. When set it
    # is used INSTEAD of ADMIN_PASSWORD, so the plain password no longer has
    # to live in the environment. Generate one with:
    #     python scripts/hash_admin_password.py
    admin_password_hash: str = ""

    # How long an admin login stays valid (hours), regardless of cookie age.
    admin_session_hours: int = 12

    # Escape hatch for LOCAL development only: allows logging in while
    # ADMIN_PASSWORD is still the built-in "changeme".
    allow_insecure_admin: bool = False

    # Comma-separated origins that may call GET /api/ping from the browser
    # (the wake-up gateway page), e.g. "https://gateway.example.com".
    # Empty = no cross-origin access.
    gateway_origins: str = ""

    # Mark the session cookie "Secure" (HTTPS only). None = auto: on when
    # running on Render (RENDER env var is set), off for local http testing.
    session_https_only: bool | None = None

    # Used to cryptographically sign the session cookie. Set this to a long
    # random string in production (e.g. `openssl rand -hex 32`).
    secret_key: str = "insecure-dev-secret-change-me"

    # Same Telegram bot token used by the bot service -- needed so the
    # dashboard can call Telegram's sendMessage API for broadcasts.
    bot_token: str = ""

    # Bot's @username (no @), needed by the Telegram Login Widget on the
    # community register/login pages -- it's public info, not a secret.
    bot_username: str = ""

    # --- Last.fm (powers the "Зараз слухаю" widget on /profile) ---
    # Free API key from https://www.last.fm/api/account/create -- no
    # subscription needed. lastfm_username is whatever Last.fm account has
    # Spotify scrobbling turned on (Spotify app -> Settings -> Apps -> Last.fm).
    lastfm_api_key: str = ""
    lastfm_username: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def async_database_url(self) -> str:
        """
        Return a connection string SQLAlchemy's async engine can use.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

        # SQLAlchemy passes query parameters to asyncpg as keyword arguments.
        # asyncpg expects ``ssl`` rather than ``sslmode`` in that code path.
        url = re.sub(r"([?&])sslmode=([^&]+)", r"\1ssl=\2", url, flags=re.IGNORECASE)
        return url


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance so we only parse the environment once."""
    return Settings()
