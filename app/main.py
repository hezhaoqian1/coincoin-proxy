import asyncio
import logging
from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .admin import router as admin_router
from .auth import router as auth_router
from .keys import router as keys_router
from .proxy import router as proxy_router, close_http_client
from .openai_compat import router as openai_router
from .webhook import router as webhook_router
from .payment import router as payment_router
from .config import settings
from .db import Base, engine
from .usage_buffer import flush_loop, flush_once


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s: %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

WEB_DIR = Path(__file__).parent.parent / "static" / "web"


async def _run_migrations(conn):
    """Add columns introduced after initial create_all (safe to re-run)."""
    from sqlalchemy import text
    migrations = [
        ("coincoin_payment_orders", "trade_no", "VARCHAR(128) NULL"),
        ("coincoin_payment_orders", "pay_url", "VARCHAR(512) NULL"),
        ("coincoin_api_keys", "kind", "VARCHAR(16) DEFAULT 'api'"),
        ("coincoin_api_keys", "expires_at", "DATETIME NULL"),
        ("coincoin_users", "referral_code", "VARCHAR(16) NULL UNIQUE"),
        ("coincoin_users", "referred_by", "VARCHAR(32) NULL"),
    ]
    for table, col, ddl in migrations:
        try:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_migrations(conn)

    flush_task = asyncio.create_task(flush_loop(settings.usage_flush_interval))
    logging.info("CoinCoin Proxy started")

    try:
        yield
    finally:
        flush_task.cancel()
        await flush_once()
        await close_http_client()
        logging.info("CoinCoin Proxy stopped")


app = FastAPI(
    title="CoinCoin Proxy",
    description="OpenAI Compatible API Proxy for Azure OpenAI",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(proxy_router)
app.include_router(openai_router)
app.include_router(keys_router)
app.include_router(admin_router)
app.include_router(webhook_router)
app.include_router(payment_router)
app.include_router(auth_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "coincoin-proxy"}


if WEB_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="web-assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(request: Request, full_path: str):
        file_path = WEB_DIR / full_path
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(WEB_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
