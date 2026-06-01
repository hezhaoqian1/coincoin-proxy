from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class KeyActivateRequest(BaseModel):
    username: Optional[str] = None
    external_id: Optional[str] = None
    force_new: bool = False


class KeyActivateResponse(BaseModel):
    user_id: str
    api_key: str
    status: str


class DeveloperKeySummary(BaseModel):
    key_id: str
    masked_key: str
    name: str = ""
    created_at: datetime
    last_used_at: Optional[datetime] = None
    status: str
    expires_at: Optional[datetime] = None


class DeveloperKeyStateResponse(BaseModel):
    has_active_key: bool
    active_key_count: int = 0
    latest_key: Optional[DeveloperKeySummary] = None


class DeveloperKeyListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key_id: str
    masked_key: str
    api_key: Optional[str] = None
    name: str = ""
    purpose: str = ""
    status: str
    expires_at: Optional[datetime] = None
    monthly_quota_cents: Optional[int] = None
    total_quota_cents: Optional[int] = None
    monthly_used_cents: int = 0
    total_used_cents: int = 0
    ip_allowlist: List[str] = []
    created_at: datetime
    last_used_at: Optional[datetime] = None


class DeveloperKeyListResponse(BaseModel):
    total: int
    active: int
    disabled: int
    data: List[DeveloperKeyListItem]


class DeveloperKeyCreateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key_id: str
    api_key: str
    masked_key: str
    name: str = ""
    purpose: str = ""
    status: str
    expires_at: Optional[datetime] = None
    monthly_quota_cents: Optional[int] = None
    total_quota_cents: Optional[int] = None
    ip_allowlist: List[str] = []
    created_at: datetime


class DeveloperKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=100)
    purpose: Optional[str] = Field(default=None, max_length=255)
    expires_at: Optional[datetime] = None
    monthly_quota_cents: Optional[int] = Field(default=None, ge=0)
    total_quota_cents: Optional[int] = Field(default=None, ge=0)
    ip_allowlist: Optional[List[str]] = Field(default=None, max_length=50)


class DeveloperKeyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = Field(default=None, pattern=r"^(active|disabled)$")
    name: Optional[str] = Field(default=None, max_length=100)
    purpose: Optional[str] = Field(default=None, max_length=255)
    expires_at: Optional[datetime] = None
    monthly_quota_cents: Optional[int] = Field(default=None, ge=0)
    total_quota_cents: Optional[int] = Field(default=None, ge=0)
    ip_allowlist: Optional[List[str]] = Field(default=None, max_length=50)


class AdminUserUpdate(BaseModel):
    status: Optional[str] = Field(default=None, examples=["active", "blocked"])
    balance: Optional[int] = Field(default=None, description="手动调整余额（分）")
    token_limit: Optional[int] = None
    token_used: Optional[int] = Field(default=None, description="手动调整已用 token 数")
    input_tokens_used: Optional[int] = Field(default=None, description="手动调整已用输入 token 数")
    output_tokens_used: Optional[int] = Field(default=None, description="手动调整已用输出 token 数")
    request_limit_per_minute: Optional[int] = None
    request_limit_per_day: Optional[int] = None


class AdminUserPasswordResetRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128, description="新的控制台登录密码")


class AdminUserPasswordResetResponse(BaseModel):
    user_id: str
    username: Optional[str] = None
    account_status: str
    status: str = "password_reset"


class AdminKeyUpdate(BaseModel):
    status: Optional[str] = Field(default=None, examples=["active", "disabled"])


class AdminPaymentManualConfirmRequest(BaseModel):
    proof_url: str = Field(..., description="支付成功回跳 URL，需包含 out_trade_no / trade_no / money / trade_status")


class AdminSubscriptionAdjustRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(..., description="月卡产品 ID")
    status: str = Field(default="active", pattern=r"^(active|expired|disabled)$")
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    paid_until: Optional[datetime] = None
    quota_cents: Optional[int] = Field(default=None, ge=0)
    used_cents: Optional[int] = Field(default=None, ge=0)
    note: Optional[str] = Field(default="", max_length=512)


class AdminTrafficPackGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(..., description="流量包产品 ID")
    remaining_cents: Optional[int] = Field(default=None, ge=0)
    expires_at: Optional[datetime] = None
    note: Optional[str] = Field(default="", max_length=512)


class AdminTrafficPackUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = Field(default=None, pattern=r"^(active|depleted|expired|disabled)$")
    remaining_cents: Optional[int] = Field(default=None, ge=0)
    expires_at: Optional[datetime] = None
    note: Optional[str] = Field(default="", max_length=512)


class AdminModelAliasUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_alias: Optional[str] = Field(default=None, max_length=128)
    provider_model: Optional[str] = Field(default=None, max_length=128)
    upstream_model: Optional[str] = Field(default=None, max_length=128)
    enabled: Optional[bool] = None


class AdminModelPricingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_multiplier: Optional[float] = Field(default=None, ge=0)
    output_multiplier: Optional[float] = Field(default=None, ge=0)
    cache_read_multiplier: Optional[float] = Field(default=None, ge=0)
    image_multiplier: Optional[float] = Field(default=None, ge=0)


class AdminProviderChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    provider_platform: str = Field(default="", max_length=64)
    channel_type: str = Field(default="openai_compatible", max_length=32)
    base_url: str = Field(..., min_length=1, max_length=512)
    api_key: str = Field(..., min_length=1, max_length=4096)
    auth_style: str = Field(default="bearer", pattern=r"^(bearer|azure)$")
    status: str = Field(default="active", pattern=r"^(active|disabled)$")
    priority: int = Field(default=0, ge=0)
    weight: int = Field(default=1, ge=1)
    allowed_fails: int = Field(default=3, ge=1)
    cooldown_seconds: float = Field(default=30.0, ge=0)
    capabilities: List[str] = Field(default_factory=list, max_length=16)
    provider_account_fingerprint: str = Field(default="", max_length=128)
    cost_tier: str = Field(default="", max_length=32)
    notes: str = Field(default="", max_length=2048)


class AdminProviderChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    provider_platform: Optional[str] = Field(default=None, max_length=64)
    channel_type: Optional[str] = Field(default=None, max_length=32)
    base_url: Optional[str] = Field(default=None, min_length=1, max_length=512)
    api_key: Optional[str] = Field(default=None, min_length=1, max_length=4096)
    auth_style: Optional[str] = Field(default=None, pattern=r"^(bearer|azure)$")
    status: Optional[str] = Field(default=None, pattern=r"^(active|disabled)$")
    priority: Optional[int] = Field(default=None, ge=0)
    weight: Optional[int] = Field(default=None, ge=1)
    allowed_fails: Optional[int] = Field(default=None, ge=1)
    cooldown_seconds: Optional[float] = Field(default=None, ge=0)
    capabilities: Optional[List[str]] = Field(default=None, max_length=16)
    provider_account_fingerprint: Optional[str] = Field(default=None, max_length=128)
    cost_tier: Optional[str] = Field(default=None, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=2048)


class AdminModelChannelRouteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_model_id: str = Field(..., min_length=1, max_length=128)
    endpoint: str = Field(default="", max_length=64)
    channel_id: str = Field(..., min_length=1, max_length=32)
    upstream_model: str = Field(default="", max_length=128)
    priority_override: Optional[int] = Field(default=None, ge=0)
    weight_override: Optional[int] = Field(default=None, ge=1)
    transform_profile: str = Field(default="openai_compatible", max_length=64)
    status: str = Field(default="active", pattern=r"^(active|disabled)$")
    notes: str = Field(default="", max_length=2048)


class AdminModelChannelRouteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_model_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    endpoint: Optional[str] = Field(default=None, max_length=64)
    channel_id: Optional[str] = Field(default=None, min_length=1, max_length=32)
    upstream_model: Optional[str] = Field(default=None, max_length=128)
    priority_override: Optional[int] = Field(default=None, ge=0)
    weight_override: Optional[int] = Field(default=None, ge=1)
    transform_profile: Optional[str] = Field(default=None, max_length=64)
    status: Optional[str] = Field(default=None, pattern=r"^(active|disabled)$")
    notes: Optional[str] = Field(default=None, max_length=2048)


class AdminClaudeCompatSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., min_length=1, max_length=32)


class RechargeRequest(BaseModel):
    """充值请求"""
    order_id: str = Field(..., description="外部订单号，用于幂等性校验")
    user_id: Optional[str] = Field(default=None, description="用户 ID (u_xxx)")
    username: Optional[str] = Field(default=None, description="用户名")
    external_id: Optional[str] = Field(default=None, description="外部用户 ID")
    amount: Optional[int] = Field(default=None, description="支付金额（分），仅记录用")
    add_balance: int = Field(default=0, description="增加的余额（分）")
    add_tokens: int = Field(default=0, description="增加的 token 额度（兼容旧逻辑）")
    add_daily_requests: int = Field(default=0, description="增加的每日请求限额")
    note: Optional[str] = Field(default=None, description="备注")


class RechargeResponse(BaseModel):
    """充值响应"""
    success: bool
    order_id: str
    user_id: str
    balance: int = Field(description="充值后的余额（分）")
    token_limit: Optional[int] = Field(default=None, description="充值后的 token 限额")
    request_limit_per_day: Optional[int] = Field(default=None, description="充值后的每日请求限额")
    message: str


class BalanceResponse(BaseModel):
    """余额查询响应"""
    user_id: str
    balance: int = Field(description="账户余额（分，即 0.01 美元）")
    balance_usd: float = Field(description="账户余额（美元）")
    token_used: int = Field(description="已用 tokens 总量")
    input_tokens_used: int = Field(description="已用输入 tokens")
    output_tokens_used: int = Field(description="已用输出 tokens")
    token_limit: Optional[int] = Field(default=None, description="Token 限额（null 表示无限）")
    token_remaining: Optional[int] = Field(default=None, description="剩余 tokens（null 表示无限）")
    price_input_per_million: float = Field(description="输入价格（美元/百万 tokens）")
    price_cached_input_per_million: float = Field(description="缓存输入价格（美元/百万 tokens）")
    price_output_per_million: float = Field(description="输出价格（美元/百万 tokens）")
    pricing_scope: str = Field(default="official", description="official / station")
    pricing_model_id: Optional[str] = Field(default=None, description="当前简表采用的模型或站长别名")
    station_id: Optional[str] = None
    station_slug: Optional[str] = None
    station_display_name: Optional[str] = None
    station_pricing_models: Optional[List[dict]] = None
    billing: Optional[dict] = None


class ReferralCodeUpdateRequest(BaseModel):
    referral_code: str = Field(..., min_length=4, max_length=16, pattern=r'^[A-Za-z0-9]+$')


# ===== Payment =====

class OrderCreateRequest(BaseModel):
    money: str = Field(..., description="支付金额（元），如 '9.90'")
    name: str = Field(default="CoinCoin 充值", description="商品名称")
    pay_type: str = Field(default="alipay", description="支付方式: alipay / wxpay")
    product_id: Optional[str] = Field(default=None, description="预设套餐或流量包 ID")

class OrderCreateResponse(BaseModel):
    order_no: str
    pay_url: str
    amount_rmb: str
    expected_cents: int = Field(description="预计充值余额（分）")

class OrderConfirmRequest(BaseModel):
    order_no: str = Field(..., description="proxy 侧订单号")
    proof_url: Optional[str] = Field(default=None, description="支付成功回跳 URL，包含签名与支付结果参数")

class OrderConfirmResponse(BaseModel):
    success: bool
    order_no: str
    amount_rmb: str
    added_cents: int = Field(description="充值金额（分）")
    new_balance: int = Field(description="充值后余额（分）")
    new_balance_usd: float
    available_cents: Optional[int] = Field(default=None, description="充值后可用总额（分，含套餐、流量包、历史余额）")
    available_usd: Optional[float] = Field(default=None, description="充值后可用总额（美元）")
    billing_action: Optional[str] = Field(default=None, description="本次入账动作")
    message: str


# ===== Redemption =====

class RedeemRequest(BaseModel):
    code: str = Field(..., description="兑换码")

class RedeemResponse(BaseModel):
    success: bool
    added_cents: int
    new_balance: int
    new_balance_usd: float
    message: str

class RedemptionGenerateRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100, description="生成数量")
    balance_cents: int = Field(..., ge=1, description="每张面额（分）")

class RedemptionGenerateResponse(BaseModel):
    codes: List[str]
    balance_cents: int
    count: int


# ===== Announcements =====

class AnnouncementCreate(BaseModel):
    title: str
    content: str
    priority: str = Field(default="info", description="info / warning / critical")
    display_type: Literal["banner", "modal"] = Field(default="banner", description="banner / modal")
    audience: Literal["all", "signup"] = Field(default="all", description="all / signup")
    cta_label: Optional[str] = None
    cta_value: Optional[str] = None
    image_url: Optional[str] = None

class AnnouncementUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[str] = None
    display_type: Optional[Literal["banner", "modal"]] = None
    audience: Optional[Literal["all", "signup"]] = None
    cta_label: Optional[str] = None
    cta_value: Optional[str] = None
    image_url: Optional[str] = None
    status: Optional[str] = Field(default=None, description="active / archived")


# ===== Auth =====

class AuthRegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64, pattern=r'^[a-zA-Z0-9_.-]+$')
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=6, max_length=128)
    referral_code: Optional[str] = Field(default=None, description="邀请码（可选）")
    station_slug: Optional[str] = Field(default=None, min_length=1, max_length=64, description="站长入口 slug（可选）")
    verification_id: Optional[str] = Field(default=None, description="预注册邮箱验证会话 ID")
    verification_code: Optional[str] = Field(default=None, min_length=4, max_length=12, description="邮箱验证码")


class AuthRegisterSendCodeRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)


class AuthRegisterSendCodeResponse(BaseModel):
    verification_id: str
    email: str
    status: str = "code_sent"


class AuthRegisterCheckCodeRequest(BaseModel):
    verification_id: str
    code: str = Field(..., min_length=4, max_length=12)


class AuthRegisterCheckCodeResponse(BaseModel):
    verification_id: str
    email: str
    verified: bool = True
    status: str = "verified"

class AuthLoginRequest(BaseModel):
    username: str
    password: str
    station_slug: Optional[str] = Field(default=None, min_length=1, max_length=64, description="站长入口 slug（可选）")

class AuthResponse(BaseModel):
    user_id: str
    username: str
    session_key: str = Field(description="kind=session key for Dashboard access only")

class AuthRegisterResponse(BaseModel):
    user_id: str
    username: str
    email: str
    status: str = "email_verification_required"
    session_key: Optional[str] = Field(default=None, description="present only when verification is not required")

class AuthVerifyEmailRequest(BaseModel):
    user_id: str
    code: str = Field(..., min_length=4, max_length=12)

class AuthResendEmailRequest(BaseModel):
    user_id: str

class AuthProfileResponse(BaseModel):
    user_id: str
    username: Optional[str] = None
    email: Optional[str] = None
    email_verified_at: Optional[datetime] = None
    email_verification_required: bool = False

class AuthSendEmailCodeRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)

class AuthVerifyCurrentEmailRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=12)


class AuthChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


class AuthChangePasswordResponse(BaseModel):
    status: str = "password_updated"


# ===== Station Center =====

class StationApplicationCreateRequest(BaseModel):
    station_name: str = Field(..., min_length=2, max_length=128)
    contact_handle: str = Field(default="", max_length=128)
    traffic_source: str = Field(default="", max_length=256)
    audience_note: str = Field(..., min_length=10, max_length=5000)
    settlement_method: str = Field(default="alipay_manual")
    settlement_payee_name: str = Field(default="", max_length=128)
    settlement_payee_account: str = Field(default="", max_length=128)
    settlement_qr_url: str = Field(default="", max_length=512)


class StationApplicationReviewRequest(BaseModel):
    status: str = Field(..., description="approved / rejected")
    review_note: str = Field(default="", max_length=5000)


class AdminStationCreateRequest(BaseModel):
    owner_user_id: Optional[str] = Field(default=None, max_length=64)
    owner_username: Optional[str] = Field(default=None, max_length=128)
    owner_email: Optional[str] = Field(default=None, max_length=255)
    display_name: str = Field(..., min_length=2, max_length=128)
    slug: Optional[str] = Field(default=None, max_length=64)
    mode: str = Field(default="commission_station")
    commission_rate: float = Field(default=0.15, ge=0, le=1)
    balance_cents: int = Field(default=0, ge=0)
    wholesale_tier: str = Field(default="standard", max_length=32)
    settlement_method: str = Field(default="alipay_manual")
    settlement_payee_name: str = Field(default="", max_length=128)
    settlement_payee_account: str = Field(default="", max_length=128)
    settlement_qr_url: str = Field(default="", max_length=512)
    create_default_alias: bool = Field(default=False)
    default_alias: str = Field(default="fast", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    default_target_public_model_id: str = Field(default="", max_length=128)
    default_capability: str = Field(default="chat/completions", max_length=64)
    retail_input_per_million_cents: int = Field(default=0, ge=0)
    retail_output_per_million_cents: int = Field(default=0, ge=0)
    retail_price_per_image_cents: float = Field(default=0, ge=0)


class StationPayoutBatchCreateRequest(BaseModel):
    station_id: str
    notes: Optional[str] = Field(default=None, max_length=5000)


class StationPayoutBatchMarkPaidRequest(BaseModel):
    payment_reference: str = Field(default="", max_length=128)
    payment_screenshot_url: str = Field(default="", max_length=512)
    payment_note: str = Field(default="", max_length=5000)


class StationCustomerCreateRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64, pattern=r'^[a-zA-Z0-9_.-]+$')
    create_api_key: bool = Field(default=True)


class StationSettlementUpdateRequest(BaseModel):
    settlement_method: str = Field(default="alipay_manual")
    settlement_payee_name: str = Field(default="", max_length=128)
    settlement_payee_account: str = Field(default="", max_length=128)
    settlement_qr_url: str = Field(default="", max_length=512)


class StationAliasCreateRequest(BaseModel):
    alias: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    target_public_model_id: str = Field(..., min_length=1, max_length=128)
    fallback_target_public_model_id: str = Field(default="", max_length=128)
    capability: str = Field(default="chat/completions", max_length=64)
    retail_input_per_million_cents: int = Field(default=0, ge=0)
    retail_output_per_million_cents: int = Field(default=0, ge=0)
    retail_price_per_image_cents: float = Field(default=0, ge=0)
    is_default_text: bool = Field(default=False)
    is_default_image: bool = Field(default=False)


class StationAliasUpdateRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(active|disabled)$")
    target_public_model_id: Optional[str] = Field(default=None, max_length=128)
    fallback_target_public_model_id: Optional[str] = Field(default=None, max_length=128)
    is_default_text: Optional[bool] = None
    is_default_image: Optional[bool] = None


class StationPricebookUpdateRequest(BaseModel):
    retail_input_per_million_cents: Optional[int] = Field(default=None, ge=0)
    retail_output_per_million_cents: Optional[int] = Field(default=None, ge=0)
    retail_price_per_image_cents: Optional[float] = Field(default=None, ge=0)
    status: Optional[str] = Field(default=None, pattern=r"^(active|disabled)$")


class StationBrandingUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=128)
    logo_url: Optional[str] = Field(default=None, max_length=512)
    favicon_url: Optional[str] = Field(default=None, max_length=512)
    support_email: Optional[str] = Field(default=None, max_length=255)
    support_link: Optional[str] = Field(default=None, max_length=512)
    docs_intro: Optional[str] = Field(default=None, max_length=5000)
    terms_url: Optional[str] = Field(default=None, max_length=512)
