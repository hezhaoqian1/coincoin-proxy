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
from .channel_monitoring import provider_channel_monitor_loop
from .image_jobs import (
    image_job_loop,
    openai_router as image_jobs_openai_router,
    router as image_jobs_router,
)
from .media_artifacts import router as media_artifacts_router
from .video_jobs import (
    openai_router as video_jobs_openai_router,
    router as video_jobs_router,
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
from .model_alias_overrides import get_model_alias_override_db_state, refresh_model_alias_registry_from_db
from .model_pricing_overrides import get_model_pricing_override_db_state, refresh_model_pricing_registry_from_db
from .provider_channels import get_provider_channel_db_state, refresh_provider_channel_router_from_db
from .system_settings import get_runtime_system_settings_db_state, refresh_runtime_system_settings_from_db
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
        ("coincoin_payment_orders", "product_id", "VARCHAR(64) DEFAULT ''"),
        ("coincoin_payment_orders", "station_id", "VARCHAR(32) NULL"),
        ("coincoin_payment_orders", "station_owner_user_id", "VARCHAR(32) NULL"),
        ("coincoin_payment_orders", "station_commission_rate", "DOUBLE DEFAULT 0"),
        ("coincoin_payment_orders", "station_commission_rmb_cents", "BIGINT DEFAULT 0"),
        ("coincoin_payment_orders", "station_payout_status", "VARCHAR(16) DEFAULT 'none'"),
        ("coincoin_api_keys", "kind", "VARCHAR(16) DEFAULT 'api'"),
        ("coincoin_api_keys", "name", "VARCHAR(100) DEFAULT ''"),
        ("coincoin_api_keys", "purpose", "VARCHAR(255) DEFAULT ''"),
        ("coincoin_api_keys", "monthly_quota_cents", "BIGINT NULL"),
        ("coincoin_api_keys", "total_quota_cents", "BIGINT NULL"),
        ("coincoin_api_keys", "ip_allowlist", "TEXT NULL"),
        ("coincoin_api_keys", "expires_at", "DATETIME NULL"),
        ("coincoin_api_keys", "encrypted_key", "LONGTEXT NULL"),
        ("coincoin_users", "referral_code", "VARCHAR(16) NULL UNIQUE"),
        ("coincoin_users", "referred_by", "VARCHAR(32) NULL"),
        ("coincoin_users", "register_ip", "VARCHAR(64) NULL"),
        ("coincoin_users", "email", "VARCHAR(255) NULL UNIQUE"),
        ("coincoin_users", "email_verified_at", "DATETIME NULL"),
        ("coincoin_referral_rewards", "recipient_id", "VARCHAR(32) NULL"),
        ("coincoin_referral_rewards", "reward_type", "VARCHAR(32) DEFAULT 'purchase_commission'"),
        ("coincoin_referral_rewards", "idempotency_key", "VARCHAR(128) NULL UNIQUE"),
        ("coincoin_accounts", "status", "VARCHAR(32) DEFAULT 'active'"),
        ("coincoin_request_logs", "cached_tokens", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "cache_read_tokens", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "cache_creation_tokens", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "api_key_id", "VARCHAR(32) NULL"),
        ("coincoin_request_logs", "route_reason", "VARCHAR(64) DEFAULT ''"),
        ("coincoin_usage_daily", "images_total", "BIGINT DEFAULT 0"),
        ("coincoin_usage_daily", "videos_total", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "image_count", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "video_count", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "provider_model", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "customer_model_alias", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "usage_unit_type", "VARCHAR(32) DEFAULT 'tokens'"),
        ("coincoin_request_logs", "usage_unit_count", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "billable_sku", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "upstream_request_id", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "channel_id", "VARCHAR(32) DEFAULT ''"),
        ("coincoin_request_logs", "channel_type", "VARCHAR(32) DEFAULT ''"),
        ("coincoin_request_logs", "provider_platform", "VARCHAR(64) DEFAULT ''"),
        ("coincoin_request_logs", "provider_account_fingerprint", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "fallback_from_channel_id", "VARCHAR(32) DEFAULT ''"),
        ("coincoin_request_logs", "route_attempt", "BIGINT DEFAULT 0"),
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
        ("coincoin_image_jobs", "api_key_id", "VARCHAR(32) NULL"),
        ("coincoin_video_jobs", "api_key_id", "VARCHAR(32) NULL"),
        ("coincoin_video_jobs", "subscription_debit_cents", "BIGINT DEFAULT 0"),
        ("coincoin_video_jobs", "subscription_id", "VARCHAR(32) DEFAULT ''"),
        ("coincoin_video_jobs", "subscription_plan_id", "VARCHAR(64) DEFAULT ''"),
        ("coincoin_video_jobs", "traffic_pack_debit_cents", "BIGINT DEFAULT 0"),
        ("coincoin_video_jobs", "traffic_pack_debits_json", "TEXT NULL"),
        ("coincoin_video_jobs", "legacy_debit_cents", "BIGINT DEFAULT 0"),
        ("coincoin_stations", "commission_rate", "DOUBLE DEFAULT 0.15"),
        ("coincoin_stations", "mode", "VARCHAR(32) DEFAULT 'commission_station'"),
        ("coincoin_stations", "balance_cents", "BIGINT DEFAULT 0"),
        ("coincoin_stations", "currency", "VARCHAR(16) DEFAULT 'usd_cents'"),
        ("coincoin_stations", "wholesale_tier", "VARCHAR(32) DEFAULT 'standard'"),
        ("coincoin_stations", "allowed_catalog_bundle", "TEXT NULL"),
        ("coincoin_stations", "default_text_alias", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_stations", "default_image_alias", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_stations", "request_limit_per_minute", "BIGINT NULL"),
        ("coincoin_stations", "daily_spend_limit_cents", "BIGINT NULL"),
        ("coincoin_stations", "monthly_spend_limit_cents", "BIGINT NULL"),
        ("coincoin_stations", "suspended_reason", "VARCHAR(512) DEFAULT ''"),
        ("coincoin_request_logs", "station_id", "VARCHAR(32) DEFAULT ''"),
        ("coincoin_request_logs", "station_alias", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "resolved_public_model", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_request_logs", "wholesale_cost_cents", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "retail_charge_cents", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "price_version", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "pricing_mode", "VARCHAR(32) DEFAULT ''"),
        ("coincoin_request_logs", "model_multiplier", "DOUBLE DEFAULT 1"),
        ("coincoin_request_logs", "output_multiplier", "DOUBLE DEFAULT 1"),
        ("coincoin_request_logs", "cache_read_multiplier", "DOUBLE DEFAULT 0"),
        ("coincoin_request_logs", "image_multiplier", "DOUBLE DEFAULT 1"),
        ("coincoin_request_logs", "video_multiplier", "DOUBLE DEFAULT 1"),
        ("coincoin_request_logs", "base_price_input_per_million", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "base_price_output_per_million", "BIGINT DEFAULT 0"),
        ("coincoin_request_logs", "base_price_per_image_cents", "DOUBLE DEFAULT 0"),
        ("coincoin_request_logs", "base_price_per_video_cents", "DOUBLE DEFAULT 0"),
        ("coincoin_request_logs", "price_per_video_cents", "DOUBLE DEFAULT 0"),
        ("coincoin_request_logs", "effective_cached_input_per_million", "DOUBLE DEFAULT 0"),
        ("coincoin_station_payout_batches", "payment_reference", "VARCHAR(128) DEFAULT ''"),
        ("coincoin_station_payout_batches", "payment_screenshot_url", "VARCHAR(512) DEFAULT ''"),
        ("coincoin_station_payout_batches", "payment_note", "TEXT NULL"),
        ("coincoin_announcements", "display_type", "VARCHAR(16) DEFAULT 'banner'"),
        ("coincoin_announcements", "audience", "VARCHAR(32) DEFAULT 'all'"),
        ("coincoin_announcements", "cta_label", "VARCHAR(64) DEFAULT ''"),
        ("coincoin_announcements", "cta_value", "VARCHAR(512) DEFAULT ''"),
        ("coincoin_announcements", "image_url", "VARCHAR(512) DEFAULT ''"),
        ("coincoin_redemption_codes", "max_redemptions", "BIGINT DEFAULT 1"),
        ("coincoin_redemption_codes", "per_user_limit", "BIGINT DEFAULT 1"),
        ("coincoin_redemption_codes", "redemption_count", "BIGINT DEFAULT 0"),
        ("coincoin_redemption_codes", "note", "VARCHAR(256) DEFAULT ''"),
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

    index_migrations = [
        ("coincoin_request_logs", "ix_request_logs_created_at", "CREATE INDEX ix_request_logs_created_at ON coincoin_request_logs (created_at)"),
        ("coincoin_request_logs", "ix_request_logs_created_model", "CREATE INDEX ix_request_logs_created_model ON coincoin_request_logs (created_at, model)"),
        ("coincoin_payment_orders", "ix_payment_orders_confirmed_at", "CREATE INDEX ix_payment_orders_confirmed_at ON coincoin_payment_orders (confirmed_at)"),
        ("coincoin_api_keys", "ix_api_keys_created_at", "CREATE INDEX ix_api_keys_created_at ON coincoin_api_keys (created_at)"),
    ]
    for table, index_name, ddl in index_migrations:
        try:
            await conn.execute(text(ddl))
            logger.info("index migration OK: %s.%s", table, index_name)
        except Exception as exc:
            exc_msg = str(exc).lower()
            if "duplicate" in exc_msg or "already exists" in exc_msg:
                logger.debug("index %s.%s already exists, skipping", table, index_name)
            else:
                logger.warning("index migration failed for %s.%s: %s", table, index_name, exc)

    table_migrations = [
        """
        CREATE TABLE coincoin_user_subscriptions (
            id VARCHAR(32) PRIMARY KEY,
            user_id VARCHAR(32) NOT NULL UNIQUE,
            plan_id VARCHAR(64) DEFAULT '',
            status VARCHAR(16) DEFAULT 'active',
            period_start DATETIME NULL,
            period_end DATETIME NULL,
            paid_until DATETIME NULL,
            quota_cents BIGINT DEFAULT 0,
            used_cents BIGINT DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_user_subscriptions_user_id (user_id),
            INDEX ix_user_subscriptions_status (status),
            INDEX ix_user_subscriptions_paid_until (paid_until)
        )
        """,
        """
        CREATE TABLE coincoin_traffic_pack_balances (
            id VARCHAR(32) PRIMARY KEY,
            user_id VARCHAR(32) NOT NULL,
            product_id VARCHAR(64) DEFAULT '',
            status VARCHAR(16) DEFAULT 'active',
            original_cents BIGINT DEFAULT 0,
            remaining_cents BIGINT DEFAULT 0,
            expires_at DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_traffic_pack_balances_user_id (user_id),
            INDEX ix_traffic_pack_balances_product_id (product_id),
            INDEX ix_traffic_pack_balances_status (status),
            INDEX ix_traffic_pack_balances_expires_at (expires_at)
        )
        """,
        """
        CREATE TABLE coincoin_billing_ledger (
            id VARCHAR(32) PRIMARY KEY,
            user_id VARCHAR(32) NOT NULL,
            entry_type VARCHAR(32) DEFAULT '',
            amount_cents BIGINT DEFAULT 0,
            source_type VARCHAR(32) DEFAULT '',
            source_id VARCHAR(128) DEFAULT '',
            product_id VARCHAR(64) DEFAULT '',
            balance_after_cents BIGINT DEFAULT 0,
            note VARCHAR(512) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_billing_ledger_user_id (user_id),
            INDEX ix_billing_ledger_entry_type (entry_type),
            INDEX ix_billing_ledger_source_id (source_id),
            INDEX ix_billing_ledger_product_id (product_id),
            INDEX ix_billing_ledger_created_at (created_at)
        )
        """,
        """
        CREATE TABLE coincoin_redemption_code_uses (
            id VARCHAR(32) PRIMARY KEY,
            code_id VARCHAR(32) NOT NULL,
            code VARCHAR(32) NOT NULL,
            user_id VARCHAR(32) NOT NULL,
            balance_cents BIGINT DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_redemption_code_uses_code_id (code_id),
            INDEX ix_redemption_code_uses_code (code),
            INDEX ix_redemption_code_uses_user_id (user_id),
            INDEX ix_redemption_code_uses_created_at (created_at),
            INDEX ix_redemption_code_uses_code_user (code_id, user_id)
        )
        """,
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
        """
        CREATE TABLE coincoin_model_alias_overrides (
            alias_id VARCHAR(128) PRIMARY KEY,
            provider_model VARCHAR(128) DEFAULT '',
            upstream_model VARCHAR(128) DEFAULT '',
            enabled BIGINT DEFAULT 1,
            updated_by VARCHAR(64) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_model_alias_overrides_updated_at (updated_at)
        )
        """,
        """
        CREATE TABLE coincoin_model_pricing_overrides (
            model_id VARCHAR(128) PRIMARY KEY,
            model_multiplier DOUBLE DEFAULT 1,
            output_multiplier DOUBLE DEFAULT 1,
            cache_read_multiplier DOUBLE DEFAULT 0,
            image_multiplier DOUBLE DEFAULT 1,
            video_multiplier DOUBLE DEFAULT 1,
            pricing_mode VARCHAR(32) DEFAULT 'multiplier',
            price_version BIGINT DEFAULT 1,
            updated_by VARCHAR(64) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_model_pricing_overrides_updated_at (updated_at)
        )
        """,
        """
        CREATE TABLE coincoin_video_jobs (
            id VARCHAR(32) PRIMARY KEY,
            user_id VARCHAR(32) NOT NULL,
            api_key_id VARCHAR(32) NULL,
            status VARCHAR(16) DEFAULT 'queued',
            endpoint VARCHAR(32) DEFAULT 'videos/generations',
            public_model VARCHAR(128) DEFAULT '',
            provider_model VARCHAR(128) DEFAULT '',
            route_reason VARCHAR(64) DEFAULT '',
            upstream_task_id VARCHAR(128) DEFAULT '',
            request_payload_json TEXT NOT NULL,
            result_payload_json LONGTEXT NULL,
            error_code VARCHAR(64) DEFAULT '',
            error_message TEXT NULL,
            upstream_request_id VARCHAR(128) DEFAULT '',
            channel_id VARCHAR(32) DEFAULT '',
            channel_type VARCHAR(32) DEFAULT '',
            provider_platform VARCHAR(64) DEFAULT '',
            provider_account_fingerprint VARCHAR(128) DEFAULT '',
            charged_cents BIGINT DEFAULT 0,
            refunded_cents BIGINT DEFAULT 0,
            subscription_debit_cents BIGINT DEFAULT 0,
            subscription_id VARCHAR(32) DEFAULT '',
            subscription_plan_id VARCHAR(64) DEFAULT '',
            traffic_pack_debit_cents BIGINT DEFAULT 0,
            traffic_pack_debits_json TEXT NULL,
            legacy_debit_cents BIGINT DEFAULT 0,
            attempt_count BIGINT DEFAULT 0,
            duration_ms BIGINT DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME NULL,
            completed_at DATETIME NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_video_jobs_user_id (user_id),
            INDEX ix_video_jobs_api_key_id (api_key_id),
            INDEX ix_video_jobs_status (status),
            INDEX ix_video_jobs_upstream_task_id (upstream_task_id),
            INDEX ix_video_jobs_channel_id (channel_id),
            INDEX ix_video_jobs_created_at (created_at),
            INDEX ix_video_jobs_status_created (status, created_at)
        )
        """,
        """
        CREATE TABLE coincoin_media_artifacts (
            id VARCHAR(32) PRIMARY KEY,
            user_id VARCHAR(32) NOT NULL,
            api_key_id VARCHAR(32) NULL,
            media_type VARCHAR(16) DEFAULT '',
            endpoint VARCHAR(64) DEFAULT '',
            model VARCHAR(128) DEFAULT '',
            provider_model VARCHAR(128) DEFAULT '',
            status VARCHAR(16) DEFAULT 'completed',
            url VARCHAR(2048) DEFAULT '',
            thumbnail_url VARCHAR(2048) DEFAULT '',
            source_type VARCHAR(32) DEFAULT '',
            source_id VARCHAR(128) DEFAULT '',
            upstream_request_id VARCHAR(128) DEFAULT '',
            route_reason VARCHAR(64) DEFAULT '',
            cost_cents BIGINT DEFAULT 0,
            metadata_json TEXT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME NULL,
            INDEX ix_media_artifacts_user_id (user_id),
            INDEX ix_media_artifacts_api_key_id (api_key_id),
            INDEX ix_media_artifacts_media_type (media_type),
            INDEX ix_media_artifacts_status (status),
            INDEX ix_media_artifacts_source_id (source_id),
            INDEX ix_media_artifacts_created_at (created_at),
            INDEX ix_media_artifacts_user_created (user_id, created_at),
            INDEX ix_media_artifacts_user_type_created (user_id, media_type, created_at)
        )
        """,
        """
        CREATE TABLE coincoin_station_aliases (
            id VARCHAR(32) PRIMARY KEY,
            station_id VARCHAR(32) NOT NULL,
            alias VARCHAR(128) NOT NULL,
            target_public_model_id VARCHAR(128) DEFAULT '',
            fallback_target_public_model_id VARCHAR(128) DEFAULT '',
            capability VARCHAR(64) DEFAULT 'chat/completions',
            status VARCHAR(16) DEFAULT 'active',
            is_default_text BIGINT DEFAULT 0,
            is_default_image BIGINT DEFAULT 0,
            metadata_json TEXT NULL,
            created_by_user_id VARCHAR(32) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY ix_station_alias_unique (station_id, alias),
            INDEX ix_station_aliases_station_id (station_id),
            INDEX ix_station_aliases_status (status),
            INDEX ix_station_aliases_created_at (created_at)
        )
        """,
        """
        CREATE TABLE coincoin_station_pricebook (
            id VARCHAR(32) PRIMARY KEY,
            station_id VARCHAR(32) NOT NULL,
            station_alias_id VARCHAR(32) NOT NULL,
            billable_sku VARCHAR(128) DEFAULT '',
            usage_unit_type VARCHAR(32) DEFAULT 'tokens',
            retail_input_per_million_cents BIGINT DEFAULT 0,
            retail_output_per_million_cents BIGINT DEFAULT 0,
            retail_price_per_image_cents DOUBLE DEFAULT 0,
            min_allowed_cents BIGINT DEFAULT 0,
            max_allowed_cents BIGINT DEFAULT 0,
            price_version BIGINT DEFAULT 1,
            status VARCHAR(16) DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_station_pricebook_station_id (station_id),
            INDEX ix_station_pricebook_station_alias_id (station_alias_id),
            INDEX ix_station_pricebook_billable_sku (billable_sku),
            INDEX ix_station_pricebook_status (status),
            INDEX ix_station_pricebook_created_at (created_at)
        )
        """,
        """
        CREATE TABLE coincoin_station_ledger_entries (
            id VARCHAR(32) PRIMARY KEY,
            station_id VARCHAR(32) NOT NULL,
            entry_type VARCHAR(32) DEFAULT 'adjustment',
            amount_cents BIGINT DEFAULT 0,
            balance_after_cents BIGINT DEFAULT 0,
            reference_type VARCHAR(64) DEFAULT '',
            reference_id VARCHAR(128) DEFAULT '',
            request_log_id VARCHAR(32) DEFAULT '',
            notes TEXT NULL,
            created_by_user_id VARCHAR(32) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_station_ledger_station_id (station_id),
            INDEX ix_station_ledger_entry_type (entry_type),
            INDEX ix_station_ledger_created_at (created_at)
        )
        """,
        """
        CREATE TABLE coincoin_station_branding (
            station_id VARCHAR(32) PRIMARY KEY,
            display_name VARCHAR(128) DEFAULT '',
            logo_url VARCHAR(512) DEFAULT '',
            favicon_url VARCHAR(512) DEFAULT '',
            support_email VARCHAR(255) DEFAULT '',
            support_link VARCHAR(512) DEFAULT '',
            docs_intro TEXT NULL,
            terms_url VARCHAR(512) DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE coincoin_system_settings (
            setting_key VARCHAR(128) PRIMARY KEY,
            setting_value TEXT NOT NULL,
            updated_by VARCHAR(64) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_system_settings_updated_at (updated_at)
        )
        """,
        """
        CREATE TABLE coincoin_provider_channels (
            id VARCHAR(32) PRIMARY KEY,
            name VARCHAR(128) DEFAULT '',
            provider_platform VARCHAR(64) DEFAULT '',
            channel_type VARCHAR(32) DEFAULT 'openai_compatible',
            base_url VARCHAR(512) DEFAULT '',
            encrypted_api_key LONGTEXT NULL,
            auth_style VARCHAR(32) DEFAULT 'bearer',
            status VARCHAR(16) DEFAULT 'active',
            priority BIGINT DEFAULT 0,
            weight BIGINT DEFAULT 1,
            allowed_fails BIGINT DEFAULT 3,
            cooldown_seconds DOUBLE DEFAULT 30,
            capabilities TEXT NULL,
            provider_account_fingerprint VARCHAR(128) DEFAULT '',
            cost_tier VARCHAR(32) DEFAULT '',
            notes TEXT NULL,
            updated_by VARCHAR(64) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_provider_channels_provider_platform (provider_platform),
            INDEX ix_provider_channels_channel_type (channel_type),
            INDEX ix_provider_channels_status (status),
            INDEX ix_provider_channels_priority (priority),
            INDEX ix_provider_channels_fingerprint (provider_account_fingerprint),
            INDEX ix_provider_channels_created_at (created_at),
            INDEX ix_provider_channels_updated_at (updated_at)
        )
        """,
        """
        CREATE TABLE coincoin_model_channel_routes (
            id VARCHAR(32) PRIMARY KEY,
            public_model_id VARCHAR(128) DEFAULT '',
            endpoint VARCHAR(64) DEFAULT '',
            channel_id VARCHAR(32) NOT NULL,
            upstream_model VARCHAR(128) DEFAULT '',
            priority_override BIGINT NULL,
            weight_override BIGINT NULL,
            transform_profile VARCHAR(64) DEFAULT 'openai_compatible',
            status VARCHAR(16) DEFAULT 'active',
            notes TEXT NULL,
            updated_by VARCHAR(64) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_model_channel_routes_public_model (public_model_id),
            INDEX ix_model_channel_routes_endpoint (endpoint),
            INDEX ix_model_channel_routes_channel_id (channel_id),
            INDEX ix_model_channel_routes_status (status),
            INDEX ix_model_channel_routes_created_at (created_at),
            INDEX ix_model_channel_routes_updated_at (updated_at)
        )
        """,
        """
        CREATE TABLE coincoin_provider_channel_runtime_state (
            channel_id VARCHAR(32) PRIMARY KEY,
            fail_count BIGINT DEFAULT 0,
            cooldown_until DATETIME NULL,
            last_success_at DATETIME NULL,
            last_failure_at DATETIME NULL,
            last_error_code VARCHAR(64) DEFAULT '',
            last_error_message VARCHAR(512) DEFAULT '',
            rolling_latency_ms BIGINT DEFAULT 0,
            rolling_failure_rate DOUBLE DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_channel_runtime_cooldown_until (cooldown_until),
            INDEX ix_channel_runtime_updated_at (updated_at)
        )
        """,
        """
        CREATE TABLE coincoin_provider_channel_monitors (
            id VARCHAR(32) PRIMARY KEY,
            channel_id VARCHAR(32) NOT NULL,
            name VARCHAR(128) DEFAULT '',
            endpoint VARCHAR(64) DEFAULT 'responses',
            primary_model VARCHAR(128) DEFAULT '',
            extra_models TEXT NULL,
            status VARCHAR(16) DEFAULT 'active',
            interval_seconds BIGINT DEFAULT 300,
            timeout_seconds BIGINT DEFAULT 30,
            last_checked_at DATETIME NULL,
            last_status VARCHAR(16) DEFAULT '',
            last_latency_ms BIGINT DEFAULT 0,
            last_ping_latency_ms BIGINT DEFAULT 0,
            last_message VARCHAR(512) DEFAULT '',
            created_by VARCHAR(64) DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_channel_monitors_channel_id (channel_id),
            INDEX ix_channel_monitors_endpoint (endpoint),
            INDEX ix_channel_monitors_status (status),
            INDEX ix_channel_monitors_last_checked_at (last_checked_at),
            INDEX ix_channel_monitors_created_at (created_at),
            INDEX ix_channel_monitors_updated_at (updated_at)
        )
        """,
        """
        CREATE TABLE coincoin_provider_channel_monitor_history (
            id VARCHAR(32) PRIMARY KEY,
            monitor_id VARCHAR(32) NOT NULL,
            channel_id VARCHAR(32) NOT NULL,
            model VARCHAR(128) DEFAULT '',
            status VARCHAR(16) DEFAULT 'error',
            latency_ms BIGINT DEFAULT 0,
            ping_latency_ms BIGINT DEFAULT 0,
            status_code BIGINT DEFAULT 0,
            message VARCHAR(512) DEFAULT '',
            checked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_channel_monitor_history_monitor (monitor_id),
            INDEX ix_channel_monitor_history_channel (channel_id),
            INDEX ix_channel_monitor_history_model (model),
            INDEX ix_channel_monitor_history_status (status),
            INDEX ix_channel_monitor_history_checked_at (checked_at)
        )
        """,
        """
        CREATE TABLE coincoin_provider_channel_monitor_daily (
            id VARCHAR(32) PRIMARY KEY,
            monitor_id VARCHAR(32) NOT NULL,
            channel_id VARCHAR(32) NOT NULL,
            model VARCHAR(128) DEFAULT '',
            bucket_date DATE NOT NULL,
            total_checks BIGINT DEFAULT 0,
            operational_count BIGINT DEFAULT 0,
            degraded_count BIGINT DEFAULT 0,
            failed_count BIGINT DEFAULT 0,
            error_count BIGINT DEFAULT 0,
            sum_latency_ms BIGINT DEFAULT 0,
            count_latency BIGINT DEFAULT 0,
            sum_ping_latency_ms BIGINT DEFAULT 0,
            count_ping_latency BIGINT DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY ix_channel_monitor_daily_unique (monitor_id, model, bucket_date),
            INDEX ix_channel_monitor_daily_monitor (monitor_id),
            INDEX ix_channel_monitor_daily_channel (channel_id),
            INDEX ix_channel_monitor_daily_model (model),
            INDEX ix_channel_monitor_daily_bucket (bucket_date),
            INDEX ix_channel_monitor_daily_updated_at (updated_at)
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

    index_migrations = [
        "CREATE INDEX ix_request_logs_user_key_created ON coincoin_request_logs (user_id, api_key_id, created_at)",
        "CREATE INDEX ix_request_logs_station_created ON coincoin_request_logs (station_id, created_at)",
        "CREATE INDEX ix_request_logs_channel_created ON coincoin_request_logs (channel_id, created_at)",
        "CREATE INDEX ix_request_logs_platform_created ON coincoin_request_logs (provider_platform, created_at)",
        "CREATE INDEX ix_channel_monitor_history_channel_checked ON coincoin_provider_channel_monitor_history (channel_id, checked_at)",
        "CREATE INDEX ix_channel_monitor_daily_channel_bucket ON coincoin_provider_channel_monitor_daily (channel_id, bucket_date)",
        "CREATE INDEX ix_referral_rewards_recipient_id ON coincoin_referral_rewards (recipient_id)",
        "CREATE INDEX ix_referral_rewards_reward_type ON coincoin_referral_rewards (reward_type)",
    ]
    for sql in index_migrations:
        try:
            await conn.execute(text(sql))
            logger.info("index migration OK: %s", sql)
        except Exception as exc:
            exc_msg = str(exc).lower()
            if "duplicate" in exc_msg or "already exists" in exc_msg:
                logger.debug("index already exists, skipping: %s", sql)
            else:
                logger.warning("index migration failed for [%s]: %s", sql, exc)

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

    try:
        await conn.execute(text("""
            INSERT INTO coincoin_redemption_code_uses
                (id, code_id, code, user_id, balance_cents, created_at)
            SELECT
                CONCAT('rcu_', SUBSTRING(MD5(CONCAT(id, ':', used_by)), 1, 24)),
                id,
                code,
                used_by,
                balance_cents,
                used_at
            FROM coincoin_redemption_codes
            WHERE used_by IS NOT NULL
              AND used_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM coincoin_redemption_code_uses u
                  WHERE u.code_id = coincoin_redemption_codes.id
                    AND u.user_id = coincoin_redemption_codes.used_by
              )
        """))
        logger.info("redemption use backfill OK")
    except Exception as exc:
        logger.warning("redemption use backfill failed: %s", exc)

    try:
        await conn.execute(text("""
            INSERT INTO coincoin_redemption_codes
                (id, code, balance_cents, status, max_redemptions, per_user_limit, redemption_count, note)
            VALUES
                ('rc_libertytalk0607', 'libertytalk0607', 10000, 'active', 0, 1, 0, 'LibertyTalk 0607 campaign')
            ON DUPLICATE KEY UPDATE code = code
        """))
        logger.info("redemption campaign seed OK: libertytalk0607")
    except Exception as exc:
        logger.warning("redemption campaign seed failed: %s", exc)


async def model_alias_override_refresh_loop(interval_seconds: int):
    from .db import SessionLocal

    logger = logging.getLogger("coincoin.model_alias_overrides")
    last_state = None
    while True:
        try:
            async with SessionLocal() as db:
                state = await get_model_alias_override_db_state(db)
                if state != last_state:
                    await refresh_model_alias_registry_from_db(db)
                    last_state = state
                    logger.info("refreshed model alias overrides from database count=%s", state[0])
        except Exception as exc:
            logger.warning("failed to refresh model alias overrides from database: %s", exc)
        await asyncio.sleep(max(1, int(interval_seconds or 10)))


async def model_pricing_override_refresh_loop(interval_seconds: int):
    from .db import SessionLocal

    logger = logging.getLogger("coincoin.model_pricing_overrides")
    last_state = None
    while True:
        try:
            async with SessionLocal() as db:
                state = await get_model_pricing_override_db_state(db)
                if state != last_state:
                    await refresh_model_pricing_registry_from_db(db)
                    last_state = state
                    logger.info("refreshed model pricing overrides from database count=%s", state[0])
        except Exception as exc:
            logger.warning("failed to refresh model pricing overrides from database: %s", exc)
        await asyncio.sleep(max(1, int(interval_seconds or 10)))


async def runtime_system_settings_refresh_loop(interval_seconds: int):
    from .db import SessionLocal

    logger = logging.getLogger("coincoin.system_settings")
    last_state = None
    while True:
        try:
            async with SessionLocal() as db:
                state = await get_runtime_system_settings_db_state(db)
                if state != last_state:
                    await refresh_runtime_system_settings_from_db(db)
                    last_state = state
                    logger.info("refreshed runtime system settings from database count=%s", state[0])
        except Exception as exc:
            logger.warning("failed to refresh runtime system settings from database: %s", exc)
        await asyncio.sleep(max(1, int(interval_seconds or 10)))


async def provider_channel_refresh_loop(interval_seconds: int):
    from .db import SessionLocal

    logger = logging.getLogger("coincoin.provider_channels")
    last_state = None
    while True:
        try:
            async with SessionLocal() as db:
                state = await get_provider_channel_db_state(db)
                if state != last_state:
                    await refresh_provider_channel_router_from_db(db)
                    last_state = state
                    logger.info("refreshed provider channels from database channels=%s routes=%s", state[0], state[1])
        except Exception as exc:
            logger.warning("failed to refresh provider channels from database: %s", exc)
        await asyncio.sleep(max(1, int(interval_seconds or 10)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_migrations(conn)
    # Initialize router registry after settings/env are loaded and DB is ready.
    model_registry.init_from_settings()
    try:
        from .db import SessionLocal
        async with SessionLocal() as db:
            await refresh_runtime_system_settings_from_db(db)
            await refresh_provider_channel_router_from_db(db)
            await refresh_model_alias_registry_from_db(db)
            await refresh_model_pricing_registry_from_db(db)
    except Exception as exc:
        logging.getLogger("coincoin.model_alias_overrides").warning(
            "initial database runtime refresh failed: %s", exc
        )

    flush_task = asyncio.create_task(flush_loop(settings.usage_flush_interval))
    reconcile_task = asyncio.create_task(reconcile_loop())
    runtime_system_settings_task = asyncio.create_task(
        runtime_system_settings_refresh_loop(settings.model_alias_overrides_refresh_interval)
    )
    provider_channel_task = asyncio.create_task(
        provider_channel_refresh_loop(settings.model_alias_overrides_refresh_interval)
    )
    provider_channel_monitor_task = asyncio.create_task(
        provider_channel_monitor_loop(settings.provider_channel_monitor_poll_interval)
    )
    alias_override_task = asyncio.create_task(
        model_alias_override_refresh_loop(settings.model_alias_overrides_refresh_interval)
    )
    pricing_override_task = asyncio.create_task(
        model_pricing_override_refresh_loop(settings.model_alias_overrides_refresh_interval)
    )
    image_job_task = None
    if settings.image_jobs_enabled:
        image_job_task = asyncio.create_task(image_job_loop(settings.image_job_poll_interval))
    logging.info("CoinCoin Proxy started")

    try:
        yield
    finally:
        flush_task.cancel()
        reconcile_task.cancel()
        runtime_system_settings_task.cancel()
        provider_channel_task.cancel()
        provider_channel_monitor_task.cancel()
        alias_override_task.cancel()
        pricing_override_task.cancel()
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
app.include_router(video_jobs_router)
app.include_router(video_jobs_openai_router)
app.include_router(media_artifacts_router)
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
