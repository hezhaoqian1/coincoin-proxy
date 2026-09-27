import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, AsyncIterator
from urllib.parse import urlparse, urlunparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine
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


_lock_logger = logging.getLogger("coincoin.db")


@asynccontextmanager
async def mysql_named_lock(conn: AsyncConnection, name: str, timeout_seconds: int) -> AsyncIterator[bool]:
    """Hold a MySQL ``GET_LOCK`` for the duration of the block.

    Used to serialize startup DDL across uvicorn workers/replicas (the MySQL
    counterpart of sub2api's ``pg_try_advisory_lock`` around migrations).
    Named locks are session-scoped, so DDL implicit commits do not release it.
    Yields False (and proceeds unlocked, as before) on non-MySQL dialects or
    when the lock cannot be obtained within ``timeout_seconds``.
    """
    if conn.dialect.name != "mysql":
        yield False
        return
    lock_name = name[:64]
    acquired = False
    try:
        result = await conn.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": lock_name, "timeout": max(0, int(timeout_seconds))},
        )
        acquired = result.scalar() == 1
    except Exception:  # noqa: BLE001
        _lock_logger.warning("could not acquire MySQL lock %s; continuing unlocked", lock_name, exc_info=True)
    if not acquired:
        _lock_logger.warning("MySQL lock %s not acquired within %ss; continuing unlocked", lock_name, timeout_seconds)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                await conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
            except Exception:  # noqa: BLE001
                _lock_logger.warning("failed to release MySQL lock %s", lock_name, exc_info=True)
