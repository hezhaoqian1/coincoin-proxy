from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import HTTPException, Request, status

from .config import settings
from .quota_client import quota_reservation_client, quota_reservation_configured
from .security import generate_id


logger = logging.getLogger("coincoin.quota_lifecycle")


@dataclass
class QuotaReservationState:
    reservation_id: str
    user_id: str
    api_key_id: str = ""
    station_id: str = ""
    released: bool = False
    usage_tasks: List[Any] = field(default_factory=list)


_current_reservation: contextvars.ContextVar[Optional[QuotaReservationState]] = contextvars.ContextVar(
    "coincoin_quota_reservation",
    default=None,
)


def current_quota_reservation_id() -> str:
    state = _current_reservation.get()
    if state:
        return state.reservation_id
    return ""


async def reserve_quota_for_request(
    request: Request,
    user: Any,
    *,
    available_balance_cents: int = 0,
    estimated_cost_cents: int = 0,
) -> None:
    if not quota_reservation_configured():
        return

    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return
    api_key_id = str(getattr(user, "_api_key_id", "") or "")
    station_context = getattr(user, "_station_context", None)
    station_id = str(station_context.get("station_id") or "") if isinstance(station_context, dict) else ""

    rpm_limits = _rpm_limits_for_user(user)
    concurrency_limits = _concurrency_limits_for_request(user_id, api_key_id, station_id)
    estimated_cost_cents = max(0, int(estimated_cost_cents or 0))
    if not rpm_limits and not concurrency_limits and estimated_cost_cents <= 0:
        return

    reservation_id = generate_id("qres_")
    decision = await quota_reservation_client.reserve(
        reservation_id=reservation_id,
        user_id=user_id,
        api_key_id=api_key_id,
        station_id=station_id,
        estimated_cost_cents=estimated_cost_cents,
        available_balance_cents=max(0, int(available_balance_cents or 0)),
        rpm_limits=rpm_limits,
        concurrency_limits=concurrency_limits,
        ttl_seconds=max(1, int(settings.quota_reservation_ttl_seconds or 120)),
    )
    if not decision.allowed:
        raise _quota_denied(decision.reason)
    if decision.fail_open or not decision.reservation_id:
        return

    state = QuotaReservationState(
        reservation_id=decision.reservation_id,
        user_id=user_id,
        api_key_id=api_key_id,
        station_id=station_id,
    )
    _current_reservation.set(state)
    request.state.quota_reservation_id = decision.reservation_id


async def release_current_quota_reservation() -> None:
    await wait_for_current_quota_usage_tasks()
    state = _current_reservation.get()
    if not state or state.released:
        return
    state.released = True
    try:
        decision = await quota_reservation_client.release(state.reservation_id)
        if not decision.allowed and not decision.fail_open:
            logger.warning(
                "quota reservation release was not accepted",
                extra={"reservation_id": state.reservation_id, "reason": decision.reason},
            )
    except Exception:
        logger.exception("quota reservation release failed", extra={"reservation_id": state.reservation_id})


def register_current_quota_usage_task(task: Any) -> None:
    state = _current_reservation.get()
    if state is not None:
        state.usage_tasks.append(task)


async def wait_for_current_quota_usage_tasks() -> None:
    state = _current_reservation.get()
    if not state or not state.usage_tasks:
        return
    tasks = list(state.usage_tasks)
    state.usage_tasks.clear()
    for task in tasks:
        try:
            await task
        except Exception:
            logger.exception("quota-linked usage task failed")


async def commit_current_quota_reservation(actual_cost_cents: int = 0) -> None:
    state = _current_reservation.get()
    if not state or state.released:
        return
    state.released = True
    try:
        decision = await quota_reservation_client.commit(
            state.reservation_id,
            actual_cost_cents=max(0, int(actual_cost_cents or 0)),
        )
        if not decision.allowed and not decision.fail_open:
            logger.warning(
                "quota reservation commit was not accepted",
                extra={"reservation_id": state.reservation_id, "reason": decision.reason},
            )
            state.released = False
    except Exception:
        state.released = False
        logger.exception("quota reservation commit failed", extra={"reservation_id": state.reservation_id})


def clear_current_quota_reservation() -> None:
    _current_reservation.set(None)


class QuotaReservationASGIMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[..., Awaitable[Dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        completed = False

        async def send_wrapper(message: Dict[str, Any]) -> None:
            nonlocal completed
            await send(message)
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                completed = True
                await release_current_quota_reservation()
                clear_current_quota_reservation()

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if not completed:
                await release_current_quota_reservation()
                clear_current_quota_reservation()


def _rpm_limits_for_user(user: Any) -> List[Dict[str, Any]]:
    limit = getattr(user, "request_limit_per_minute", None)
    if limit is None:
        return []
    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        return []
    if limit_value <= 0:
        return []
    return [{"dimension": "user", "id": str(getattr(user, "id", "") or ""), "limit": limit_value, "window_seconds": 60}]


def _concurrency_limits_for_request(user_id: str, api_key_id: str, station_id: str) -> List[Dict[str, Any]]:
    limits: List[Dict[str, Any]] = []
    if int(settings.quota_user_concurrency_limit or 0) > 0:
        limits.append({"dimension": "user", "id": user_id, "limit": int(settings.quota_user_concurrency_limit)})
    if api_key_id and int(settings.quota_api_key_concurrency_limit or 0) > 0:
        limits.append({"dimension": "api_key", "id": api_key_id, "limit": int(settings.quota_api_key_concurrency_limit)})
    if station_id and int(settings.quota_station_concurrency_limit or 0) > 0:
        limits.append({"dimension": "station", "id": station_id, "limit": int(settings.quota_station_concurrency_limit)})
    return limits


def _quota_denied(reason: str) -> HTTPException:
    reason = str(reason or "quota_denied")
    if reason == "balance_reserved_exceeded":
        return HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient reserved balance")
    if reason.startswith("rpm_exceeded"):
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded")
    if reason.startswith("concurrency_exceeded"):
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="concurrency limit exceeded")
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)
