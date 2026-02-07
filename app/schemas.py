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
    token_limit: Optional[int] = None
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
    amount: Optional[int] = Field(default=None, description="金额（分），仅记录用")
    add_tokens: int = Field(default=0, description="增加的 token 额度")
    add_daily_requests: int = Field(default=0, description="增加的每日请求限额")
    note: Optional[str] = Field(default=None, description="备注")


class RechargeResponse(BaseModel):
    """充值响应"""
    success: bool
    order_id: str
    user_id: str
    token_limit: Optional[int] = Field(description="充值后的 token 限额")
    request_limit_per_day: Optional[int] = Field(description="充值后的每日请求限额")
    message: str
