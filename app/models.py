from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, String
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
    external_id: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    token_limit: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    token_used: Mapped[int] = mapped_column(BigInteger, default=0)
    request_limit_per_minute: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    request_limit_per_day: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    keys = relationship("ApiKey", back_populates="user")


class ApiKey(Base):
    __tablename__ = "coincoin_api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="keys")


class UsageDaily(Base):
    __tablename__ = "coincoin_usage_daily"

    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    tokens_total: Mapped[int] = mapped_column(BigInteger, default=0)
    requests_total: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RechargeLog(Base):
    """充值记录表，用于对账和幂等性"""
    __tablename__ = "coincoin_recharge_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)  # 外部订单号，幂等 key
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("coincoin_users.id"), index=True)
    amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # 金额（分）
    tokens_added: Mapped[int] = mapped_column(BigInteger, default=0)  # 增加的 token 额度
    daily_requests_added: Mapped[int] = mapped_column(BigInteger, default=0)  # 增加的每日请求数
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)  # 备注
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
