from typing import Optional

from pydantic import BaseModel, Field


class KeyActivateRequest(BaseModel):
    username: Optional[str] = None
    external_id: Optional[str] = None
    force_new: bool = False


class KeyActivateResponse(BaseModel):
    user_id: str
    api_key: str
    status: str


class AdminUserUpdate(BaseModel):
    status: Optional[str] = Field(default=None, examples=["active", "blocked"])
    balance: Optional[int] = Field(default=None, description="手动调整余额（分）")
    token_limit: Optional[int] = None
    token_used: Optional[int] = Field(default=None, description="手动调整已用 token 数")
    input_tokens_used: Optional[int] = Field(default=None, description="手动调整已用输入 token 数")
    output_tokens_used: Optional[int] = Field(default=None, description="手动调整已用输出 token 数")
    request_limit_per_minute: Optional[int] = None
    request_limit_per_day: Optional[int] = None


class AdminKeyUpdate(BaseModel):
    status: Optional[str] = Field(default=None, examples=["active", "disabled"])


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
    # 余额（分）
    balance: int = Field(description="账户余额（分，即 0.01 美元）")
    balance_usd: float = Field(description="账户余额（美元）")
    # Token 用量
    token_used: int = Field(description="已用 tokens 总量")
    input_tokens_used: int = Field(description="已用输入 tokens")
    output_tokens_used: int = Field(description="已用输出 tokens")
    # Token 限额
    token_limit: Optional[int] = Field(default=None, description="Token 限额（null 表示无限）")
    token_remaining: Optional[int] = Field(default=None, description="剩余 tokens（null 表示无限）")
    # 价格信息
    price_input_per_million: float = Field(description="输入价格（美元/百万 tokens）")
    price_output_per_million: float = Field(description="输出价格（美元/百万 tokens）")
