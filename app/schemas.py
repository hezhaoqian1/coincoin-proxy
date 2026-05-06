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


class AdminModelAliasUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_alias: Optional[str] = Field(default=None, max_length=128)
    provider_model: Optional[str] = Field(default=None, max_length=128)
    upstream_model: Optional[str] = Field(default=None, max_length=128)
    enabled: Optional[bool] = None


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


class ReferralCodeUpdateRequest(BaseModel):
    referral_code: str = Field(..., min_length=4, max_length=16, pattern=r'^[A-Za-z0-9]+$')


# ===== Payment =====

class OrderCreateRequest(BaseModel):
    money: str = Field(..., description="支付金额（元），如 '9.90'")
    name: str = Field(default="CoinCoin 充值", description="商品名称")
    pay_type: str = Field(default="alipay", description="支付方式: alipay / wxpay")

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
