import asyncio
import logging
from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .admin import router as admin_router
from .anthropic_compat import router as anthropic_router
from .auth import router as auth_router
from .image_jobs import (
    image_job_loop,
    openai_router as image_jobs_openai_router,
    router as image_jobs_router,
)
from .keys import router as keys_router
from .monitoring import admin_router as admin_monitoring_router, ops_router as monitoring_ops_router
from .proxy import router as proxy_router, close_http_client
from .openai_compat import (
    chat_completions as openai_chat_completions,
    embeddings as openai_embeddings,
    get_model as openai_get_model,
    list_models as openai_list_models,
    router as openai_router,
)
from .webhook import router as webhook_router
from .payment import router as payment_router
from .config import settings
from .db import Base, engine
from .usage_buffer import flush_loop, flush_once
from .reconcile import reconcile_loop
from .router import registry as model_registry
from .stations import admin_router as admin_stations_router, router as stations_router


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s: %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

WEB_DIR = Path(__file__).parent.parent / "static" / "web"
ADMIN_UPLOAD_DIR = Path(settings.admin_upload_dir)


async def _run_migrations(conn):
    """Add columns introduced after initial create_all (safe to re-run)."""
    from sqlalchemy import text
    migrations = [
        ("coincoin_payment_orders", "trade_no", "VARCHAR(128) NULL"),
        ("coincoin_payment_orders", "pay_url", "VARCHAR(512) NULL"),
        ("coincoin_payment_orders", "station_id", "VARCHAR(32) NULL"),
        ("coincoin_payment_orders", "station_owner_user_id", "VARCHAR(32) NULL"),
        ("coincoin_payment_orders", "station_commission_rate", "DOUBLE DEFAULT 0"),
        ("coincoin_payment_orders", "station_commission_rmb_cents", "BIGINT DEFAULT 0"),
        ("coincoin_payment_orders", "station_payout_status", "VARCHAR(16) DEFAULT 'none'"),
        ("coincoin_api_keys", "kind", "VARCHAR(16) DEFAULT 'api'"),
        ("coincoin_api_keys", "expires_at", "DATETIME NULL"),
        ("coincoin_api_keys", "encrypted_key", "LONGTEXT NULL"),
        ("coincoin_users", "referral_code", "VARCHAR(16) NULL UNIQUE"),
        ("coincoin_users", "referred_by", "VARCHAR(32) NULL"),
        ("coincoin_users", "register_ip", "VARCHAR(64) NULL"),
        ("coincoin_users", "email", "VARCHAR(255) NULL UNIQUE"),
        ("coincoin_users", "email_verified_at", "DATETIME NULL"),
        ("coincoin_accounts", "status", "VARCHAR(32) DEFAULT 'active'"),
        ("coincoin_request_logs", "cached_tokens", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "route_reason", "VARCHAR(64) DEFAULT ''"),
        ("coincoin_usage_daily", "images_total", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "image_count", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "provider_model", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "customer_model_alias", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "usage_unit_type", "VARCHAR(32) DEFAULT 'tokens'"),
        ("coincoin_request_logs", "usage_unit_count", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "billable_sku", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "upstream_request_id", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_user_finance_summary", "initialized_from_history", "BIGINT DEFAULT 0"),
        ("coincoin_user_finance_summary", "total_paid_rmb_cents", "BIGINT DEFAULT 0"),
        ("coincoin_user_finance_summary", "total_paid_balance_cents", "BIGINT DEFAULT 0"),
        ("coincoin_user_finance_summary", "total_ops_credit_cents", "BIGINT DEFAULT 0"),
        ("coincoin_user_finance_summary", "total_bonus_cents", "BIGINT DEFAULT 0"),
        ("coincoin_user_finance_summary", "total_consumed_cents", "BIGINT DEFAULT 0"),
        ("coincoin_user_finance_summary", "total_ops_debit_cents", "BIGINT DEFAULT 0"),
        ("coincoin_user_finance_summary", "legacy_unclassified_cents", "BIGINT DEFAULT 0"),
        ("coincoin_user_finance_summary", "total_paid_orders", "BIGINT DEFAULT 0"),
        ("coincoin_user_finance_summary", "last_payment_at", "DATETIME NULL"),
        ("coincoin_stations", "commission_rate", "DOUBLE DEFAULT 0.15"),
        ("coincoin_station_payout_batches", "payment_reference", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_station_payout_batches", "payment_screenshot_url", "VARCHAR(512) DEFAULT ''"),
        ("coincoin_station_payout_batches", "payment_note", "TEXT NULL"),
    ]
    logger = logging.getLogger("coincoin.migrations")
    for table, col, ddl in migrations:
        try:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
            logger.info("migration OK: %s.%s", table, col)
        except Exception as exc:
            exc_msg = str(exc).lower()
            if "duplicate" in exc_msg or "already exists" in exc_msg:
                logger.debug("column %s.%s already exists, skipping", table, col)
            else:
                logger.warning("migration failed for %s.%s: %s", table, col, exc)

    table_migrations = [
        """
        CREATE TABLE coincoin_station_applications (
            id VARCHAR(32) PRIMARY KEY,
            user_id VARCHAR(32) NOT NULL,
            status VARCHAR(16) DEFAULT 'pending',
            station_name VARCHAR(128) DEFAULT '',
            contact_handle VARCHAR(128) DEFAULT '',
            traffic_source VARCHAR(256) DEFAULT '',
            audience_note TEXT NOT NULL,
            settlement_method VARCHAR(32) DEFAULT 'alipay_manual',
            settlement_payee_name VARCHAR(128) DEFAULT '',
            settlement_payee_account VARCHAR(128) DEFAULT '',
            settlement_qr_url VARCHAR(512) DEFAULT '',
            review_note TEXT NULL,
            reviewed_by VARCHAR(64) DEFAULT '',
            reviewed_at DATETIME NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_station_applications_user_id (user_id),
            INDEX ix_station_applications_status (status),
            INDEX ix_station_applications_created_at (created_at)
        )
        """,
        """
        CREATE TABLE coincoin_stations (
            id VARCHAR(32) PRIMARY KEY,
            owner_user_id VARCHAR(32) NOT NULL,
            application_id VARCHAR(32) NULL UNIQUE,
            slug VARCHAR(64) NOT NULL UNIQUE,
            display_name VARCHAR(128) DEFAULT '',
            status VARCHAR(16) DEFAULT 'active',
            commission_rate DOUBLE DEFAULT 0.15,
            settlement_method VARCHAR(32) DEFAULT 'alipay_manual',
            settlement_payee_name VARCHAR(128) DEFAULT '',
            settlement_payee_account VARCHAR(128) DEFAULT '',
            settlement_qr_url VARCHAR(512) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_stations_owner_user_id (owner_user_id),
            INDEX ix_stations_status (status),
            INDEX ix_stations_created_at (created_at)
        )
        """,
        """
        CREATE TABLE coincoin_station_customer_links (
            id VARCHAR(32) PRIMARY KEY,
            station_id VARCHAR(32) NOT NULL,
            user_id VARCHAR(32) NOT NULL UNIQUE,
            created_by_user_id VARCHAR(32) NOT NULL,
            status VARCHAR(16) DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_station_customer_links_station_id (station_id),
            INDEX ix_station_customer_links_user_id (user_id),
            INDEX ix_station_customer_links_status (status),
            INDEX ix_station_customer_links_created_at (created_at)
        )
        """,
        """
        CREATE TABLE coincoin_station_commission_ledger (
            id VARCHAR(32) PRIMARY KEY,
            station_id VARCHAR(32) NOT NULL,
            user_id VARCHAR(32) NOT NULL,
            payment_order_id VARCHAR(32) NOT NULL UNIQUE,
            order_no VARCHAR(128) NOT NULL,
            status VARCHAR(16) DEFAULT 'pending',
            settlement_method VARCHAR(32) DEFAULT 'alipay_manual',
            gross_rmb_cents BIGINT DEFAULT 0,
            commission_rate DOUBLE DEFAULT 0,
            commission_rmb_cents BIGINT DEFAULT 0,
            hold_until DATETIME NULL,
            payout_batch_id VARCHAR(32) NULL,
            note TEXT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_station_commission_ledger_station_id (station_id),
            INDEX ix_station_commission_ledger_user_id (user_id),
            INDEX ix_station_commission_ledger_order_no (order_no),
            INDEX ix_station_commission_ledger_status (status),
            INDEX ix_station_commission_ledger_hold_until (hold_until),
            INDEX ix_station_commission_ledger_payout_batch_id (payout_batch_id),
            INDEX ix_station_commission_ledger_created_at (created_at)
        )
        """,
        """
        CREATE TABLE coincoin_station_payout_batches (
            id VARCHAR(32) PRIMARY KEY,
            station_id VARCHAR(32) NOT NULL,
            status VARCHAR(16) DEFAULT 'pending',
            entry_count BIGINT DEFAULT 0,
            total_commission_rmb_cents BIGINT DEFAULT 0,
            settlement_method VARCHAR(32) DEFAULT 'alipay_manual',
            payee_name VARCHAR(128) DEFAULT '',
            payee_account VARCHAR(128) DEFAULT '',
            qr_url VARCHAR(512) DEFAULT '',
            notes TEXT NULL,
            payment_reference VARCHAR(128) DEFAULT '',
            payment_screenshot_url VARCHAR(512) DEFAULT '',
            payment_note TEXT NULL,
            created_by VARCHAR(64) DEFAULT '',
            paid_by VARCHAR(64) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            paid_at DATETIME NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_station_payout_batches_station_id (station_id),
            INDEX ix_station_payout_batches_status (status),
            INDEX ix_station_payout_batches_created_at (created_at)
        )
        """,
        """
        CREATE TABLE coincoin_email_verification_codes (
            id VARCHAR(32) PRIMARY KEY,
            user_id VARCHAR(32) NOT NULL,
            email VARCHAR(255) NOT NULL,
            code_hash VARCHAR(64) NOT NULL,
            purpose VARCHAR(32) DEFAULT 'register',
            attempts BIGINT DEFAULT 0,
            expires_at DATETIME NOT NULL,
            consumed_at DATETIME NULL,
            ip_hash VARCHAR(64) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_email_verification_user_id (user_id),
            INDEX ix_email_verification_email (email),
            INDEX ix_email_verification_expires_at (expires_at),
            INDEX ix_email_verification_created_at (created_at)
        )
        """,
    ]
    for ddl in table_migrations:
        try:
            await conn.execute(text(ddl))
            logger.info("table migration OK")
        except Exception as exc:
            exc_msg = str(exc).lower()
            if "already exists" in exc_msg or "table" in exc_msg and "exists" in exc_msg:
                logger.debug("table already exists, skipping")
            else:
                logger.warning("table migration failed: %s", exc)

    cleanup_sql = [
        "DELETE FROM coincoin_email_verification_codes WHERE user_id LIKE 'regv_%'",
        "ALTER TABLE coincoin_email_verification_codes DROP FOREIGN KEY coincoin_email_verification_codes_ibfk_1",
    ]
    for sql in cleanup_sql:
        try:
            await conn.execute(text(sql))
            logger.info("email verification migration OK: %s", sql)
        except Exception as exc:
            exc_msg = str(exc).lower()
            if "check that column/key exists" in exc_msg or "can't drop" in exc_msg or "doesn't exist" in exc_msg:
                logger.debug("email verification migration skipped: %s", sql)
            elif "a foreign key constraint fails" in exc_msg:
                logger.warning("email verification cleanup skipped due to existing dependent rows: %s", sql)
            else:
                logger.warning("email verification migration failed for [%s]: %s", sql, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_migrations(conn)
    # Initialize router registry after settings/env are loaded and DB is ready.
    model_registry.init_from_settings()

    flush_task = asyncio.create_task(flush_loop(settings.usage_flush_interval))
    reconcile_task = asyncio.create_task(reconcile_loop())
    image_job_task = None
    if settings.image_jobs_enabled:
        image_job_task = asyncio.create_task(image_job_loop(settings.image_job_poll_interval))
    logging.info("CoinCoin Proxy started")

    try:
        yield
    finally:
        flush_task.cancel()
        reconcile_task.cancel()
        if image_job_task is not None:
            image_job_task.cancel()
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
app.include_router(anthropic_router)
app.include_router(openai_router)
app.include_router(image_jobs_router)
app.include_router(image_jobs_openai_router)
app.include_router(keys_router)
app.include_router(admin_router)
app.include_router(admin_monitoring_router)
app.include_router(webhook_router)
app.include_router(payment_router)
app.include_router(auth_router)
app.include_router(stations_router)
app.include_router(admin_stations_router)
app.include_router(monitoring_ops_router)

# Some OpenAI-compatible clients use `/openai/v1` as their base URL and still
# expect discovery/chat/embedding routes under that prefix.
app.add_api_route("/openai/v1/models", openai_list_models, methods=["GET"], include_in_schema=False)
app.add_api_route("/openai/v1/models/{model_id}", openai_get_model, methods=["GET"], include_in_schema=False)
app.add_api_route("/openai/v1/chat/completions", openai_chat_completions, methods=["POST"], include_in_schema=False)
app.add_api_route("/openai/v1/embeddings", openai_embeddings, methods=["POST"], include_in_schema=False)

if not ADMIN_UPLOAD_DIR.exists():
    ADMIN_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/admin-uploads", StaticFiles(directory=ADMIN_UPLOAD_DIR), name="admin-uploads")


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
