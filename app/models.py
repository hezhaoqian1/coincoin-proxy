from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .db import Base


class User(Base):
    __tablename__ = "coincoin_users"
    __table_args__ = (
        CheckConstraint(
            "(username IS NOT NULL) OR (external_id IS NOT NULL)",
            name="ck_coincoin_user_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True, nullable=True)
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    
    # 余额（单位：分，即 0.01 美元）
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    
    # Token 限制和使用量
    token_limit: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    token_used: Mapped[int] = mapped_column(BigInteger, default=0)  # 保留总量兼容
    input_tokens_used: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens_used: Mapped[int] = mapped_column(BigInteger, default=0)
    
    # 请求限制
    request_limit_per_minute: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    request_limit_per_day: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    
    # 邀请体系
    referral_code: Mapped[Optional[str]] = mapped_column(String(16), unique=True, nullable=True)
    referred_by: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("coincoin_users.id"), nullable=True)
    register_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    keys = relationship("ApiKey", back_populates="user", foreign_keys="[ApiKey.user_id]")


class ApiKey(Base):
    __tablename__ = "coincoin_api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    encrypted_key: Mapped[Optional[str]] = mapped_column(LONGTEXT, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="api")  # api / session
    name: Mapped[str] = mapped_column(String(100), default="")
    purpose: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    monthly_quota_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    total_quota_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    ip_allowlist: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="keys")


class UsageDaily(Base):
    __tablename__ = "coincoin_usage_daily"

    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    tokens_total: Mapped[int] = mapped_column(BigInteger, default=0)  # 保留兼容
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    images_total: Mapped[int] = mapped_column(BigInteger, default=0)
    videos_total: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_cents: Mapped[int] = mapped_column(BigInteger, default=0)  # 消费金额（分）
    requests_total: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RequestLog(Base):
    """请求日志表，记录每次 API 调用明细"""
    __tablename__ = "coincoin_request_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    api_key_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("coincoin_api_keys.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    endpoint: Mapped[str] = mapped_column(String(64), default="")  # chat/completions, responses, embeddings
    model: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    image_count: Mapped[int] = mapped_column(BigInteger, default=0)
    video_count: Mapped[int] = mapped_column(BigInteger, default=0)
    provider_model: Mapped[str] = mapped_column(String(128), default="")
    customer_model_alias: Mapped[str] = mapped_column(String(128), default="")
    usage_unit_type: Mapped[str] = mapped_column(String(32), default="tokens")
    usage_unit_count: Mapped[int] = mapped_column(BigInteger, default=0)
    billable_sku: Mapped[str] = mapped_column(String(128), default="")
    upstream_request_id: Mapped[str] = mapped_column(String(128), default="")
    reservation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    channel_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    channel_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    provider_platform: Mapped[str] = mapped_column(String(64), default="", index=True)
    provider_account_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    fallback_from_channel_id: Mapped[str] = mapped_column(String(32), default="")
    route_attempt: Mapped[int] = mapped_column(BigInteger, default=0)
    station_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    station_alias: Mapped[str] = mapped_column(String(128), default="")
    resolved_public_model: Mapped[str] = mapped_column(String(128), default="")
    wholesale_cost_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    retail_charge_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    price_version: Mapped[int] = mapped_column(BigInteger, default=0)
    pricing_mode: Mapped[str] = mapped_column(String(32), default="")
    model_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    output_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    cache_read_multiplier: Mapped[float] = mapped_column(Float, default=0.0)
    image_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    video_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    base_price_input_per_million: Mapped[int] = mapped_column(BigInteger, default=0)
    base_price_output_per_million: Mapped[int] = mapped_column(BigInteger, default=0)
    base_price_per_image_cents: Mapped[float] = mapped_column(Float, default=0.0)
    base_price_per_video_cents: Mapped[float] = mapped_column(Float, default=0.0)
    price_per_video_cents: Mapped[float] = mapped_column(Float, default=0.0)
    effective_cached_input_per_million: Mapped[float] = mapped_column(Float, default=0.0)
    cost_cents: Mapped[int] = mapped_column(BigInteger, default=0)  # 费用（分）
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)  # 响应耗时（毫秒）
    status_code: Mapped[int] = mapped_column(BigInteger, default=200)  # 上游响应状态码
    route_reason: Mapped[str] = mapped_column(String(64), default="")  # router decision / fallback reason


Index("ix_request_logs_user_created", RequestLog.user_id, RequestLog.created_at.desc())
Index("ix_request_logs_user_key_created", RequestLog.user_id, RequestLog.api_key_id, RequestLog.created_at.desc())


class MediaArtifact(Base):
    """Previewable media outputs for the user workbench."""
    __tablename__ = "coincoin_media_artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    api_key_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("coincoin_api_keys.id"), nullable=True, index=True)
    media_type: Mapped[str] = mapped_column(String(16), default="", index=True)
    endpoint: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    provider_model: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default="completed", index=True)
    url: Mapped[str] = mapped_column(String(2048), default="")
    thumbnail_url: Mapped[str] = mapped_column(String(2048), default="")
    source_type: Mapped[str] = mapped_column(String(32), default="")
    source_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    upstream_request_id: Mapped[str] = mapped_column(String(128), default="")
    route_reason: Mapped[str] = mapped_column(String(64), default="")
    cost_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_media_artifacts_user_created", MediaArtifact.user_id, MediaArtifact.created_at.desc())
Index("ix_media_artifacts_user_type_created", MediaArtifact.user_id, MediaArtifact.media_type, MediaArtifact.created_at.desc())


class RechargeLog(Base):
    """充值记录表，用于对账和幂等性"""
    __tablename__ = "coincoin_recharge_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    balance_added: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_added: Mapped[int] = mapped_column(BigInteger, default=0)
    daily_requests_added: Mapped[int] = mapped_column(BigInteger, default=0)
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentOrder(Base):
    """支付订单表 — 订单由 proxy 创建（pending），支付确认后变 confirmed"""
    __tablename__ = "coincoin_payment_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    order_no: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    amount_rmb: Mapped[str] = mapped_column(String(16), default="0")
    add_balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    product_id: Mapped[str] = mapped_column(String(64), default="")
    station_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    station_owner_user_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    station_commission_rate: Mapped[float] = mapped_column(Float, default=0.0)
    station_commission_rmb_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    station_payout_status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    trade_no: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    pay_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSubscription(Base):
    """Active monthly subscription state for a user."""
    __tablename__ = "coincoin_user_subscriptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), unique=True, index=True)
    plan_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    quota_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    used_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TrafficPackBalance(Base):
    """Purchased add-on balance. It can only be spent while a subscription is active."""
    __tablename__ = "coincoin_traffic_pack_balances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    original_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    remaining_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BillingLedgerEntry(Base):
    """Billing audit trail for subscription grants, add-on grants, and usage debits."""
    __tablename__ = "coincoin_billing_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    entry_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    source_type: Mapped[str] = mapped_column(String(32), default="")
    source_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    product_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    balance_after_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class RedemptionCode(Base):
    """兑换码表"""
    __tablename__ = "coincoin_redemption_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="unused")
    max_redemptions: Mapped[int] = mapped_column(BigInteger, default=1)
    per_user_limit: Mapped[int] = mapped_column(BigInteger, default=1)
    redemption_count: Mapped[int] = mapped_column(BigInteger, default=0)
    used_by: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("coincoin_users.id"), nullable=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RedemptionCodeUse(Base):
    """兑换码使用流水，用于活动码的逐用户次数限制和审计"""
    __tablename__ = "coincoin_redemption_code_uses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    code_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_redemption_codes.id"), index=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


Index("ix_redemption_code_uses_code_user", RedemptionCodeUse.code_id, RedemptionCodeUse.user_id)


class Announcement(Base):
    """公告表"""
    __tablename__ = "coincoin_announcements"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(String(2048), default="")
    priority: Mapped[str] = mapped_column(String(16), default="info")
    display_type: Mapped[str] = mapped_column(String(16), default="banner")
    audience: Mapped[str] = mapped_column(String(32), default="all")
    cta_label: Mapped[str] = mapped_column(String(64), default="")
    cta_value: Mapped[str] = mapped_column(String(512), default="")
    image_url: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelAliasOverride(Base):
    """Runtime public-model alias override. Hot request path reads an in-memory snapshot."""
    __tablename__ = "coincoin_model_alias_overrides"

    alias_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider_model: Mapped[str] = mapped_column(String(128), default="")
    upstream_model: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ModelPricingOverride(Base):
    """Runtime public-model pricing override. Hot request path reads an in-memory snapshot."""
    __tablename__ = "coincoin_model_pricing_overrides"

    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    output_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    cache_read_multiplier: Mapped[float] = mapped_column(Float, default=0.0)
    image_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    video_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    pricing_mode: Mapped[str] = mapped_column(String(32), default="multiplier")
    price_version: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class UserModelRoutingOverride(Base):
    """Admin-only per-user backend routing exception for one public model."""
    __tablename__ = "coincoin_user_model_routing_overrides"

    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), primary_key=True)
    public_model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider_model: Mapped[str] = mapped_column(String(128), default="")
    upstream_model: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class UserModelPricingOverride(Base):
    """Admin-only per-user pricing exception for one public model."""
    __tablename__ = "coincoin_user_model_pricing_overrides"

    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), primary_key=True)
    public_model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    cache_read_multiplier_override: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class SystemSetting(Base):
    """Small persistent key/value store for runtime control-plane switches."""
    __tablename__ = "coincoin_system_settings"

    setting_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    setting_value: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ProviderChannel(Base):
    """Admin-managed upstream channel used by the runtime channel router."""
    __tablename__ = "coincoin_provider_channels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    provider_platform: Mapped[str] = mapped_column(String(64), default="", index=True)
    channel_type: Mapped[str] = mapped_column(String(32), default="openai_compatible", index=True)
    base_url: Mapped[str] = mapped_column(String(512), default="")
    encrypted_api_key: Mapped[Optional[str]] = mapped_column(LONGTEXT, nullable=True)
    auth_style: Mapped[str] = mapped_column(String(32), default="bearer")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    priority: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    weight: Mapped[int] = mapped_column(BigInteger, default=1)
    allowed_fails: Mapped[int] = mapped_column(BigInteger, default=3)
    cooldown_seconds: Mapped[float] = mapped_column(Float, default=30.0)
    capabilities: Mapped[str] = mapped_column(Text, default="")
    provider_account_fingerprint: Mapped[str] = mapped_column(String(128), default="", index=True)
    cost_tier: Mapped[str] = mapped_column(String(32), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ModelChannelRoute(Base):
    """Route policy from a public model alias to one managed provider channel."""
    __tablename__ = "coincoin_model_channel_routes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    public_model_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    endpoint: Mapped[str] = mapped_column(String(64), default="", index=True)
    channel_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_provider_channels.id"), index=True)
    upstream_model: Mapped[str] = mapped_column(String(128), default="")
    priority_override: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    weight_override: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    transform_profile: Mapped[str] = mapped_column(String(64), default="openai_compatible")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ProviderChannelRuntimeState(Base):
    """Persistent health snapshot for operator visibility; hot path keeps its own memory state."""
    __tablename__ = "coincoin_provider_channel_runtime_state"

    channel_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_provider_channels.id"), primary_key=True)
    fail_count: Mapped[int] = mapped_column(BigInteger, default=0)
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str] = mapped_column(String(64), default="")
    last_error_message: Mapped[str] = mapped_column(String(512), default="")
    rolling_latency_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    rolling_failure_rate: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ProviderChannelMonitor(Base):
    """Active probe configuration for a provider channel."""
    __tablename__ = "coincoin_provider_channel_monitors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_provider_channels.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    endpoint: Mapped[str] = mapped_column(String(64), default="responses", index=True)
    primary_model: Mapped[str] = mapped_column(String(128), default="")
    extra_models: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    interval_seconds: Mapped[int] = mapped_column(BigInteger, default=300)
    timeout_seconds: Mapped[int] = mapped_column(BigInteger, default=30)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_status: Mapped[str] = mapped_column(String(16), default="")
    last_latency_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    last_ping_latency_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    last_message: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ProviderChannelMonitorHistory(Base):
    """Active probe history. One row per monitor/model/check."""
    __tablename__ = "coincoin_provider_channel_monitor_history"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    monitor_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_provider_channel_monitors.id"), index=True)
    channel_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_provider_channels.id"), index=True)
    model: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="error", index=True)
    latency_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    ping_latency_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    status_code: Mapped[int] = mapped_column(BigInteger, default=0)
    message: Mapped[str] = mapped_column(String(512), default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ProviderChannelMonitorDailyRollup(Base):
    """Daily rollup for active probe availability windows."""
    __tablename__ = "coincoin_provider_channel_monitor_daily"
    __table_args__ = (
        Index("ix_channel_monitor_daily_unique", "monitor_id", "model", "bucket_date", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    monitor_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_provider_channel_monitors.id"), index=True)
    channel_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_provider_channels.id"), index=True)
    model: Mapped[str] = mapped_column(String(128), default="", index=True)
    bucket_date: Mapped[date] = mapped_column(Date, index=True)
    total_checks: Mapped[int] = mapped_column(BigInteger, default=0)
    operational_count: Mapped[int] = mapped_column(BigInteger, default=0)
    degraded_count: Mapped[int] = mapped_column(BigInteger, default=0)
    failed_count: Mapped[int] = mapped_column(BigInteger, default=0)
    error_count: Mapped[int] = mapped_column(BigInteger, default=0)
    sum_latency_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    count_latency: Mapped[int] = mapped_column(BigInteger, default=0)
    sum_ping_latency_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    count_ping_latency: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ReferralReward(Base):
    """邀请奖励记录 — 记录邀请人和朋友各自拿到的 API 额度。"""
    __tablename__ = "coincoin_referral_rewards"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    referrer_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    referred_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"))
    recipient_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("coincoin_users.id"), nullable=True, index=True)
    reward_type: Mapped[str] = mapped_column(String(32), default="purchase_commission", index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    order_no: Mapped[str] = mapped_column(String(128), index=True)
    order_amount_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    reward_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StationApplication(Base):
    __tablename__ = "coincoin_station_applications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    station_name: Mapped[str] = mapped_column(String(128), default="")
    contact_handle: Mapped[str] = mapped_column(String(128), default="")
    traffic_source: Mapped[str] = mapped_column(String(256), default="")
    audience_note: Mapped[str] = mapped_column(Text)
    settlement_method: Mapped[str] = mapped_column(String(32), default="alipay_manual")
    settlement_payee_name: Mapped[str] = mapped_column(String(128), default="")
    settlement_payee_account: Mapped[str] = mapped_column(String(128), default="")
    settlement_qr_url: Mapped[str] = mapped_column(String(512), default="")
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(String(64), default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Station(Base):
    __tablename__ = "coincoin_stations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    application_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("coincoin_station_applications.id"), nullable=True, unique=True
    )
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    mode: Mapped[str] = mapped_column(String(32), default="commission_station", index=True)
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(16), default="usd_cents")
    wholesale_tier: Mapped[str] = mapped_column(String(32), default="standard")
    allowed_catalog_bundle: Mapped[str] = mapped_column(Text, default="")
    default_text_alias: Mapped[str] = mapped_column(String(128), default="")
    default_image_alias: Mapped[str] = mapped_column(String(128), default="")
    request_limit_per_minute: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    daily_spend_limit_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    monthly_spend_limit_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    suspended_reason: Mapped[str] = mapped_column(String(512), default="")
    commission_rate: Mapped[float] = mapped_column(Float, default=0.15)
    settlement_method: Mapped[str] = mapped_column(String(32), default="alipay_manual")
    settlement_payee_name: Mapped[str] = mapped_column(String(128), default="")
    settlement_payee_account: Mapped[str] = mapped_column(String(128), default="")
    settlement_qr_url: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StationCustomerLink(Base):
    __tablename__ = "coincoin_station_customer_links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_stations.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), unique=True, index=True)
    created_by_user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StationAlias(Base):
    __tablename__ = "coincoin_station_aliases"
    __table_args__ = (
        Index("ix_station_alias_unique", "station_id", "alias", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_stations.id"), index=True)
    alias: Mapped[str] = mapped_column(String(128), index=True)
    target_public_model_id: Mapped[str] = mapped_column(String(128), default="")
    fallback_target_public_model_id: Mapped[str] = mapped_column(String(128), default="")
    capability: Mapped[str] = mapped_column(String(64), default="chat/completions")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    is_default_text: Mapped[int] = mapped_column(BigInteger, default=0)
    is_default_image: Mapped[int] = mapped_column(BigInteger, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StationPricebookEntry(Base):
    __tablename__ = "coincoin_station_pricebook"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_stations.id"), index=True)
    station_alias_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_station_aliases.id"), index=True)
    billable_sku: Mapped[str] = mapped_column(String(128), default="", index=True)
    usage_unit_type: Mapped[str] = mapped_column(String(32), default="tokens")
    retail_input_per_million_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    retail_output_per_million_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    retail_price_per_image_cents: Mapped[float] = mapped_column(Float, default=0.0)
    min_allowed_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    max_allowed_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    price_version: Mapped[int] = mapped_column(BigInteger, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StationLedgerEntry(Base):
    __tablename__ = "coincoin_station_ledger_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_stations.id"), index=True)
    entry_type: Mapped[str] = mapped_column(String(32), default="adjustment", index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    balance_after_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    reference_type: Mapped[str] = mapped_column(String(64), default="")
    reference_id: Mapped[str] = mapped_column(String(128), default="")
    request_log_id: Mapped[str] = mapped_column(String(32), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class StationBranding(Base):
    __tablename__ = "coincoin_station_branding"

    station_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_stations.id"), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    logo_url: Mapped[str] = mapped_column(String(512), default="")
    favicon_url: Mapped[str] = mapped_column(String(512), default="")
    support_email: Mapped[str] = mapped_column(String(255), default="")
    support_link: Mapped[str] = mapped_column(String(512), default="")
    docs_intro: Mapped[str] = mapped_column(Text, default="")
    terms_url: Mapped[str] = mapped_column(String(512), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StationCommissionLedgerEntry(Base):
    __tablename__ = "coincoin_station_commission_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_stations.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    payment_order_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_payment_orders.id"), unique=True, index=True)
    order_no: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    settlement_method: Mapped[str] = mapped_column(String(32), default="alipay_manual")
    gross_rmb_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    commission_rate: Mapped[float] = mapped_column(Float, default=0.0)
    commission_rmb_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    hold_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    payout_batch_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StationPayoutBatch(Base):
    __tablename__ = "coincoin_station_payout_batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_stations.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    entry_count: Mapped[int] = mapped_column(BigInteger, default=0)
    total_commission_rmb_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    settlement_method: Mapped[str] = mapped_column(String(32), default="alipay_manual")
    payee_name: Mapped[str] = mapped_column(String(128), default="")
    payee_account: Mapped[str] = mapped_column(String(128), default="")
    qr_url: Mapped[str] = mapped_column(String(512), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    payment_reference: Mapped[str] = mapped_column(String(128), default="")
    payment_screenshot_url: Mapped[str] = mapped_column(String(512), default="")
    payment_note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    paid_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserFinanceSummary(Base):
    """按用户预聚合的财务汇总，仅供后台读取。"""
    __tablename__ = "coincoin_user_finance_summary"

    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), primary_key=True)
    initialized_from_history: Mapped[int] = mapped_column(BigInteger, default=0)
    total_paid_rmb_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    total_paid_balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    total_ops_credit_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bonus_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    total_consumed_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    total_ops_debit_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    legacy_unclassified_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    total_paid_orders: Mapped[int] = mapped_column(BigInteger, default=0)
    last_payment_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Account(Base):
    """Web 登录账号 — 与 User 通过 linked_user_id 硬绑定"""
    __tablename__ = "coincoin_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    linked_user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"))
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(BigInteger, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailVerificationCode(Base):
    """Short-lived email verification code for web registration.

    user_id stores either a real user id or a pre-registration verification id.
    It cannot be a hard foreign key because pre-registration sessions do not have
    a user row yet.
    """
    __tablename__ = "coincoin_email_verification_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32), default="register")
    attempts: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ImageJob(Base):
    __tablename__ = "coincoin_image_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    api_key_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("coincoin_api_keys.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    endpoint: Mapped[str] = mapped_column(String(32), default="images/edits")
    public_model: Mapped[str] = mapped_column(String(128), default="")
    provider_model: Mapped[str] = mapped_column(String(128), default="")
    route_reason: Mapped[str] = mapped_column(String(64), default="")
    image_count: Mapped[int] = mapped_column(BigInteger, default=0)
    request_payload_json: Mapped[str] = mapped_column(Text)
    result_payload_json: Mapped[Optional[str]] = mapped_column(LONGTEXT, nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    upstream_request_id: Mapped[str] = mapped_column(String(128), default="")
    attempt_count: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    storage_dir: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


Index("ix_image_jobs_status_created", ImageJob.status, ImageJob.created_at.desc())


class VideoJob(Base):
    __tablename__ = "coincoin_video_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    api_key_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("coincoin_api_keys.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    endpoint: Mapped[str] = mapped_column(String(32), default="videos/generations")
    public_model: Mapped[str] = mapped_column(String(128), default="")
    provider_model: Mapped[str] = mapped_column(String(128), default="")
    route_reason: Mapped[str] = mapped_column(String(64), default="")
    upstream_task_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    request_payload_json: Mapped[str] = mapped_column(Text)
    result_payload_json: Mapped[Optional[str]] = mapped_column(LONGTEXT, nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    upstream_request_id: Mapped[str] = mapped_column(String(128), default="")
    channel_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    channel_type: Mapped[str] = mapped_column(String(32), default="")
    provider_platform: Mapped[str] = mapped_column(String(64), default="")
    provider_account_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    charged_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    refunded_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    subscription_debit_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    subscription_id: Mapped[str] = mapped_column(String(32), default="")
    subscription_plan_id: Mapped[str] = mapped_column(String(64), default="")
    traffic_pack_debit_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_pack_debits_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    legacy_debit_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    attempt_count: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


Index("ix_video_jobs_status_created", VideoJob.status, VideoJob.created_at.desc())
