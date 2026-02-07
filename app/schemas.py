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
