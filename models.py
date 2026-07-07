"""
ORM models matching the required schema:
users:
  - id (primary key)
  - user_id (BigInteger, unique)
  - username (String)
  - created_at (Timestamp)
logs:
  - id (primary key)
  - user_id (BigInteger)
  - username (String)
  - action (String)
  - timestamp (Timestamp)
admin_profile:
  - id (primary key, always 1 -- single profile for now)
  - username (String)
  - avatar_url (String, nullable)
  - banner_url (String, nullable)
  - bio (Text, nullable)
  - updated_at (Timestamp)
bot_settings:
  - id (primary key, always 1 -- single settings row)
  - welcome_message (Text, nullable)
  - maintenance_mode (Boolean)
  - maintenance_message (Text, nullable)
  - updated_at (Timestamp)
"""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


def utcnow() -> datetime:
    """Timezone-aware 'now', used as a default for timestamp columns."""
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class AdminProfile(Base):
    """
    Профіль адміна. Поки завжди рівно один рядок (id=1) -- система
    спроектована так, що додати кількох адмінів пізніше буде просто
    (прибрати жорстку прив'язку до id=1 і фільтрувати по user_id сесії).

    Зберігаються лише URL-посилання на аватарку/банер, НЕ самі файли --
    це тримає таблицю на кілька байт і не залежить від ефемерного диска
    Render'а.
    """
    __tablename__ = "admin_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    banner_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class BotSettings(Base):
    """
    Загальні налаштування бота. Один рядок (id=1), як і admin_profile --
    простий і дешевий спосіб зберігати конфіг без окремої key/value таблиці.

    Сам бот (окремий процес/скрипт) читає цей рядок напряму з тієї ж
    Postgres бази перед тим, як відповісти користувачу -- жодного HTTP
    виклику до цього дашборду не потрібно.
    """
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    welcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    maintenance_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
