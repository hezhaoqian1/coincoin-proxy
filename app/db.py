from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


def _require_db_settings() -> None:
    missing = []
    if not settings.db_host:
        missing.append("COINCOIN_DB_HOST")
    if not settings.db_name:
        missing.append("COINCOIN_DB_NAME")
    if not settings.db_user:
        missing.append("COINCOIN_DB_USER")
    if not settings.db_password:
        missing.append("COINCOIN_DB_PASSWORD")
    if missing:
        raise RuntimeError(f"Missing required DB settings: {', '.join(missing)}")


_require_db_settings()


class Base(DeclarativeBase):
    pass


DATABASE_URL = (
    f"mysql+asyncmy://{settings.db_user}:{settings.db_password}"
    f"@{settings.db_host}:{settings.db_port}/{settings.db_name}?charset=utf8mb4"
)

engine = create_async_engine(
    DATABASE_URL,
    pool_size=settings.db_pool_size,
    max_overflow=20,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
