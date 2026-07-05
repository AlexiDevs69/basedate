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
    # Example (Render default form): postgres://user:pass@host:5432/dbname
    database_url: str

    # If true, tables are created automatically on app startup.
    # Convenient for development / first deploy; disable once you manage
    # schema changes with a migration tool like Alembic.
    auto_create_tables: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def async_database_url(self) -> str:
        """
        Return a connection string SQLAlchemy's async engine can use.

        Render (and most providers) hand out URLs starting with "postgres://"
        or "postgresql://". SQLAlchemy's asyncpg dialect requires the
        "postgresql+asyncpg://" prefix, so we normalize it here.
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
