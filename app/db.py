from typing import AsyncGenerator
from urllib.parse import urlparse, urlunparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


def _build_database_url() -> str:
    if settings.database_url:
        parsed = urlparse(settings.database_url)
        scheme = parsed.scheme.replace("mysql", "mysql+asyncmy", 1)
        clean = urlunparse(parsed._replace(scheme=scheme))
        if "charset" not in clean:
            clean += "&charset=utf8mb4" if "?" in clean else "?charset=utf8mb4"
        return clean

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
        raise RuntimeError(
            f"Set COINCOIN_DATABASE_URL or provide: {', '.join(missing)}"
        )

    return (
        f"mysql+asyncmy://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}?charset=utf8mb4"
    )


class Base(DeclarativeBase):
    pass


DATABASE_URL = _build_database_url()

engine = create_async_engine(
    DATABASE_URL,
    pool_size=settings.db_pool_size,
    max_overflow=20,
    pool_pre_ping=True,
    hide_parameters=True,
)

SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
