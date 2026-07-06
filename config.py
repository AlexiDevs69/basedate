"""
Application configuration.

Loads settings from environment variables (and a local .env file, if present)
using pydantic-settings. Keeping this in one place means the rest of the app
never touches os.environ directly.
"""

from functools import lru_cache

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

    # Used to cryptographically sign the session cookie. Set this to a long
    # random string in production (e.g. `openssl rand -hex 32`).
    secret_key: str = "insecure-dev-secret-change-me"

    # Same Telegram bot token used by the bot service -- needed so the
    # dashboard can call Telegram's sendMessage API for broadcasts.
    bot_token: str = ""

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
        return url


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance so we only parse the environment once."""
    return Settings()
