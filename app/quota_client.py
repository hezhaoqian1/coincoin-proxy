from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from .config import settings


logger = logging.getLogger("coincoin.quota_client")


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    reservation_id: str = ""
    reason: str = ""
    retry_after_ms: int = 0
    fail_open: bool = False


def quota_reservation_configured() -> bool:
    return bool(settings.quota_reservation_enabled and settings.quota_service_url)


class QuotaReservationClient:
    async def reserve(
        self,
        *,
        user_id: str,
        api_key_id: str = "",
        station_id: str = "",
        channel_id: str = "",
        estimated_cost_cents: int = 0,
        available_balance_cents: int = 0,
        rpm_limits: Optional[List[Dict[str, Any]]] = None,
        concurrency_limits: Optional[List[Dict[str, Any]]] = None,
        ttl_seconds: int = 120,
        reservation_id: str = "",
    ) -> QuotaDecision:
        if not quota_reservation_configured():
            return QuotaDecision(allowed=True, reason="disabled")
        payload = {
            "reservation_id": reservation_id,
            "user_id": user_id,
            "api_key_id": api_key_id,
            "station_id": station_id,
            "channel_id": channel_id,
            "estimated_cost_cents": int(estimated_cost_cents or 0),
            "available_balance_cents": int(available_balance_cents or 0),
            "rpm_limits": list(rpm_limits or []),
            "concurrency_limits": list(concurrency_limits or []),
            "ttl_seconds": int(ttl_seconds or 120),
        }
        return await self._post("/v1/quota/reserve", payload)

    async def release(self, reservation_id: str) -> QuotaDecision:
        if not quota_reservation_configured() or not reservation_id:
            return QuotaDecision(allowed=True, reservation_id=reservation_id, reason="disabled")
        return await self._post("/v1/quota/release", {"reservation_id": reservation_id})

    async def commit(self, reservation_id: str, actual_cost_cents: int = 0) -> QuotaDecision:
        if not quota_reservation_configured() or not reservation_id:
            return QuotaDecision(allowed=True, reservation_id=reservation_id, reason="disabled")
        return await self._post(
            "/v1/quota/commit",
            {"reservation_id": reservation_id, "actual_cost_cents": int(actual_cost_cents or 0)},
        )

    async def _post(self, path: str, payload: Dict[str, Any]) -> QuotaDecision:
        base_url = settings.quota_service_url.rstrip("/")
        timeout = max(0.01, float(settings.quota_service_timeout_seconds or 0.25))
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.post(f"{base_url}{path}", json=payload)
        except (httpx.TimeoutException, httpx.RequestError):
            logger.exception("quota reservation service request failed")
            if settings.quota_service_fail_open:
                return QuotaDecision(allowed=True, reason="quota_service_unavailable", fail_open=True)
            return QuotaDecision(allowed=False, reason="quota_service_unavailable")

        try:
            body = response.json()
        except ValueError:
            body = {}

        allowed = bool(body.get("allowed")) if isinstance(body, dict) else False
        reason = str(body.get("reason") or "") if isinstance(body, dict) else ""
        reservation_id = str(body.get("reservation_id") or payload.get("reservation_id") or "") if isinstance(body, dict) else ""
        retry_after_ms = int(body.get("retry_after_ms") or 0) if isinstance(body, dict) else 0
        if response.status_code >= 500 and settings.quota_service_fail_open:
            return QuotaDecision(allowed=True, reservation_id=reservation_id, reason="quota_service_error", fail_open=True)
        return QuotaDecision(
            allowed=allowed and response.status_code < 400,
            reservation_id=reservation_id,
            reason=reason or ("quota_denied" if response.status_code >= 400 else ""),
            retry_after_ms=retry_after_ms,
        )


quota_reservation_client = QuotaReservationClient()
