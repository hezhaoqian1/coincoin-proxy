from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable

import httpx
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal
from .anthropic_adapter import DEFAULT_ANTHROPIC_VERSION, build_anthropic_messages_url
from .gemini_cpa import normalize_openai_base_url
from .models import (
    ProviderChannel,
    ProviderChannelMonitor,
    ProviderChannelMonitorDailyRollup,
    ProviderChannelMonitorHistory,
)
from .security import decrypt_api_key, generate_id


logger = logging.getLogger("coincoin.channel_monitoring")

MONITOR_OK_STATUSES = {"operational", "degraded"}
MONITOR_FAILURE_STATUSES = {"failed", "error"}


@dataclass(frozen=True)
class ProbeResult:
    model: str
    status: str
    latency_ms: int
    ping_latency_ms: int
    status_code: int
    message: str
    checked_at: datetime


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def parse_monitor_models(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, list):
                return [str(item).strip() for item in loaded if str(item).strip()]
        except Exception:
            pass
    return [item.strip() for item in text.replace("\n", ",").split(",") if item.strip()]


def serialize_monitor_models(models: Iterable[str]) -> str:
    values = [str(item).strip() for item in (models or []) if str(item).strip()]
    return json.dumps(values, ensure_ascii=False)


def monitor_model_list(monitor: ProviderChannelMonitor) -> list[str]:
    primary = str(getattr(monitor, "primary_model", "") or "").strip()
    values = [primary] if primary else []
    values.extend(parse_monitor_models(getattr(monitor, "extra_models", "")))
    return list(dict.fromkeys(item for item in values if item))


def _mask_message(message: str) -> str:
    return str(message or "").replace("\n", " ").strip()[:512]


def _status_for_response(response: httpx.Response, payload: Any, latency_ms: int) -> tuple[str, str]:
    if response.status_code in {408, 409, 429} or response.status_code >= 500:
        return "failed", f"HTTP {response.status_code}"
    if response.status_code >= 400:
        return "error", f"HTTP {response.status_code}"
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return "failed", _mask_message(payload["error"].get("message") or f"HTTP {response.status_code}")
    if latency_ms >= 30_000:
        return "degraded", f"slow response {latency_ms}ms"
    return "operational", "ok"


def _headers(channel: ProviderChannel, api_key: str) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    auth_style = str(getattr(channel, "auth_style", "") or "bearer").strip().lower()
    if auth_style in {"azure", "api-key"}:
        headers["api-key"] = api_key
    elif auth_style in {"x-api-key", "anthropic_x_api_key", "anthropic"}:
        headers["x-api-key"] = api_key
    else:
        headers["authorization"] = f"Bearer {api_key}"
    if _is_anthropic_compatible_channel(channel):
        headers["anthropic-version"] = DEFAULT_ANTHROPIC_VERSION
        if _is_claude_code_only_channel(channel):
            headers.update(
                {
                    "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,context-management-2025-06-27,prompt-caching-scope-2026-01-05,effort-2025-11-24",
                    "anthropic-dangerous-direct-browser-access": "true",
                    "user-agent": "claude-cli/2.1.198 (external, sdk-cli)",
                    "x-app": "cli",
                    "x-claude-code-session-id": "coincoin-monitor",
                    "x-stainless-arch": "arm64",
                    "x-stainless-lang": "js",
                    "x-stainless-os": "MacOS",
                    "x-stainless-package-version": "0.94.0",
                    "x-stainless-runtime": "node",
                    "x-stainless-runtime-version": "v26.3.0",
                    "x-stainless-timeout": str(settings.provider_channel_monitor_default_timeout),
                }
            )
    return headers


def _is_anthropic_compatible_channel(channel: ProviderChannel) -> bool:
    return str(getattr(channel, "channel_type", "") or "").strip().lower() == "anthropic_compatible"


def _is_claude_code_only_channel(channel: ProviderChannel) -> bool:
    values = (
        getattr(channel, "cost_tier", ""),
        getattr(channel, "provider_account_fingerprint", ""),
        getattr(channel, "notes", ""),
    )
    text = " ".join(str(item or "").lower() for item in values)
    return "claude-code" in text


async def _ping_models(client: httpx.AsyncClient, base_url: str, headers: dict[str, str]) -> tuple[int, str]:
    started = time.monotonic()
    try:
        response = await client.get(f"{base_url}/models", headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            return latency_ms, f"models HTTP {response.status_code}"
        return latency_ms, ""
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return latency_ms, _mask_message(str(exc) or type(exc).__name__)


async def _probe_model(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    endpoint: str,
    model: str,
    ping_latency_ms: int,
    ping_message: str,
    channel_type: str = "",
) -> ProbeResult:
    checked_at = _utcnow()
    endpoint = (endpoint or "responses").strip()
    if channel_type == "anthropic_compatible":
        url = build_anthropic_messages_url(base_url)
        if "claude-code" in str(headers.get("anthropic-beta", "")).lower():
            url = f"{url}?beta=true"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "stream": False,
        }
    elif endpoint == "chat/completions":
        url = f"{base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "stream": False,
        }
    else:
        url = f"{base_url}/responses"
        payload = {
            "model": model,
            "input": "ping",
            "max_output_tokens": 8,
            "store": False,
            "stream": False,
        }

    started = time.monotonic()
    try:
        response = await client.post(url, json=payload, headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            data: Any = response.json()
        except ValueError:
            data = response.text
        status, message = _status_for_response(response, data, latency_ms)
        if ping_message and status == "operational":
            status = "degraded"
            message = ping_message
        return ProbeResult(
            model=model,
            status=status,
            latency_ms=latency_ms,
            ping_latency_ms=ping_latency_ms,
            status_code=int(response.status_code),
            message=message,
            checked_at=checked_at,
        )
    except httpx.TimeoutException as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return ProbeResult(model, "failed", latency_ms, ping_latency_ms, 0, _mask_message(str(exc) or "timeout"), checked_at)
    except httpx.RequestError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return ProbeResult(model, "failed", latency_ms, ping_latency_ms, 0, _mask_message(str(exc) or "request error"), checked_at)
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return ProbeResult(model, "error", latency_ms, ping_latency_ms, 0, _mask_message(str(exc) or "probe error"), checked_at)


async def run_provider_channel_monitor_once(
    db: AsyncSession,
    monitor_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[ProbeResult]:
    monitor = await db.get(ProviderChannelMonitor, monitor_id)
    if monitor is None:
        return []
    channel = await db.get(ProviderChannel, monitor.channel_id)
    now = _utcnow()
    if channel is None:
        result = ProbeResult(monitor.primary_model, "error", 0, 0, 0, "provider channel not found", now)
        await _record_monitor_results(db, monitor, [result])
        await db.commit()
        return [result]

    try:
        api_key = decrypt_api_key(getattr(channel, "encrypted_api_key", None))
    except Exception:
        api_key = ""
    if not api_key:
        result = ProbeResult(monitor.primary_model, "error", 0, 0, 0, "provider channel API key unavailable", now)
        await _record_monitor_results(db, monitor, [result])
        await db.commit()
        return [result]

    models = monitor_model_list(monitor)
    if not models:
        result = ProbeResult("", "error", 0, 0, 0, "monitor has no model", now)
        await _record_monitor_results(db, monitor, [result])
        await db.commit()
        return [result]

    channel_type = str(getattr(channel, "channel_type", "") or "").strip().lower()
    if channel_type == "anthropic_compatible":
        base_url = str(getattr(channel, "base_url", "") or "").strip().rstrip("/")
    else:
        base_url = normalize_openai_base_url(str(getattr(channel, "base_url", "") or ""))
    headers = _headers(channel, api_key)
    timeout = httpx.Timeout(
        connect=min(10.0, float(monitor.timeout_seconds or settings.provider_channel_monitor_default_timeout)),
        read=float(monitor.timeout_seconds or settings.provider_channel_monitor_default_timeout),
        write=10.0,
        pool=10.0,
    )
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        close_client = True
    try:
        if channel_type == "anthropic_compatible":
            ping_latency_ms, ping_message = 0, ""
        else:
            ping_latency_ms, ping_message = await _ping_models(client, base_url, headers)
        results = [
            await _probe_model(
                client,
                base_url=base_url,
                headers=headers,
                channel_type=channel_type,
                endpoint=monitor.endpoint,
                model=model,
                ping_latency_ms=ping_latency_ms,
                ping_message=ping_message,
            )
            for model in models
        ]
    finally:
        if close_client:
            await client.aclose()

    await _record_monitor_results(db, monitor, results)
    await db.commit()
    return results


async def _record_monitor_results(
    db: AsyncSession,
    monitor: ProviderChannelMonitor,
    results: list[ProbeResult],
) -> None:
    if not results:
        return
    primary = str(monitor.primary_model or "").strip()
    primary_result = results[0]
    for result in results:
        db.add(
            ProviderChannelMonitorHistory(
                id=generate_id("cmh_"),
                monitor_id=monitor.id,
                channel_id=monitor.channel_id,
                model=result.model,
                status=result.status,
                latency_ms=max(0, int(result.latency_ms or 0)),
                ping_latency_ms=max(0, int(result.ping_latency_ms or 0)),
                status_code=max(0, int(result.status_code or 0)),
                message=_mask_message(result.message),
                checked_at=result.checked_at,
            )
        )
        if result.model == primary:
            primary_result = result
        await _upsert_daily_rollup(db, monitor, result)

    monitor.last_checked_at = max(result.checked_at for result in results)
    monitor.last_status = primary_result.status
    monitor.last_latency_ms = max(0, int(primary_result.latency_ms or 0))
    monitor.last_ping_latency_ms = max(0, int(primary_result.ping_latency_ms or 0))
    monitor.last_message = _mask_message(primary_result.message)


async def _upsert_daily_rollup(db: AsyncSession, monitor: ProviderChannelMonitor, result: ProbeResult) -> None:
    bucket_date = result.checked_at.date()
    row = (
        await db.execute(
            select(ProviderChannelMonitorDailyRollup).where(
                ProviderChannelMonitorDailyRollup.monitor_id == monitor.id,
                ProviderChannelMonitorDailyRollup.model == result.model,
                ProviderChannelMonitorDailyRollup.bucket_date == bucket_date,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = ProviderChannelMonitorDailyRollup(
            id=generate_id("cmd_"),
            monitor_id=monitor.id,
            channel_id=monitor.channel_id,
            model=result.model,
            bucket_date=bucket_date,
        )
        db.add(row)

    row.total_checks = int(row.total_checks or 0) + 1
    if result.status == "operational":
        row.operational_count = int(row.operational_count or 0) + 1
    elif result.status == "degraded":
        row.degraded_count = int(row.degraded_count or 0) + 1
    elif result.status == "failed":
        row.failed_count = int(row.failed_count or 0) + 1
    else:
        row.error_count = int(row.error_count or 0) + 1
    if result.latency_ms > 0:
        row.sum_latency_ms = int(row.sum_latency_ms or 0) + int(result.latency_ms)
        row.count_latency = int(row.count_latency or 0) + 1
    if result.ping_latency_ms > 0:
        row.sum_ping_latency_ms = int(row.sum_ping_latency_ms or 0) + int(result.ping_latency_ms)
        row.count_ping_latency = int(row.count_ping_latency or 0) + 1


async def due_provider_channel_monitor_ids(db: AsyncSession, *, limit: int = 10) -> list[str]:
    now = _utcnow()
    rows = (
        await db.execute(
            select(ProviderChannelMonitor)
            .where(ProviderChannelMonitor.status == "active")
            .order_by(
                case((ProviderChannelMonitor.last_checked_at.is_(None), 0), else_=1).asc(),
                ProviderChannelMonitor.last_checked_at.asc(),
                ProviderChannelMonitor.created_at.asc(),
            )
            .limit(max(1, int(limit or 10)))
        )
    ).scalars().all()
    due: list[str] = []
    for row in rows:
        last_checked = row.last_checked_at
        interval = max(15, int(row.interval_seconds or settings.provider_channel_monitor_default_interval))
        if last_checked is None or last_checked <= now - timedelta(seconds=interval):
            due.append(row.id)
    return due


async def provider_channel_monitor_loop(poll_interval_seconds: int) -> None:
    if not settings.provider_channel_monitor_enabled:
        logger.info("provider channel monitor loop disabled")
        return
    await asyncio.sleep(2)
    last_cleanup_at = 0.0
    while True:
        try:
            async with SessionLocal() as db:
                monitor_ids = await due_provider_channel_monitor_ids(db, limit=10)
                for monitor_id in monitor_ids:
                    try:
                        await run_provider_channel_monitor_once(db, monitor_id)
                    except Exception as exc:
                        logger.warning("provider channel monitor failed monitor_id=%s error=%s", monitor_id, exc)
                now_monotonic = time.monotonic()
                if now_monotonic - last_cleanup_at >= 3600:
                    deleted = await cleanup_provider_channel_monitor_history(db)
                    if deleted:
                        logger.info("provider channel monitor history cleanup deleted=%s", deleted)
                    last_cleanup_at = now_monotonic
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("provider channel monitor loop failed: %s", exc)
        await asyncio.sleep(max(5, int(poll_interval_seconds or 15)))


async def cleanup_provider_channel_monitor_history(db: AsyncSession, retention_days: int | None = None) -> int:
    days = max(7, int(retention_days or settings.provider_channel_monitor_history_retention_days or 35))
    cutoff = _utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(ProviderChannelMonitorHistory)
            .where(ProviderChannelMonitorHistory.checked_at < cutoff)
            .limit(500)
        )
    ).scalars().all()
    for row in rows:
        await db.delete(row)
    await db.commit()
    return len(rows)


async def monitor_availability_rows(db: AsyncSession, *, window_days: int = 7) -> dict[str, dict[str, Any]]:
    since = date.today() - timedelta(days=max(1, int(window_days or 7)) - 1)
    rows = (
        await db.execute(
            select(
                ProviderChannelMonitorDailyRollup.monitor_id.label("monitor_id"),
                ProviderChannelMonitorDailyRollup.model.label("model"),
                func.coalesce(func.sum(ProviderChannelMonitorDailyRollup.total_checks), 0).label("total_checks"),
                func.coalesce(func.sum(ProviderChannelMonitorDailyRollup.operational_count), 0).label("operational_count"),
                func.coalesce(func.sum(ProviderChannelMonitorDailyRollup.degraded_count), 0).label("degraded_count"),
                func.coalesce(func.sum(ProviderChannelMonitorDailyRollup.failed_count), 0).label("failed_count"),
                func.coalesce(func.sum(ProviderChannelMonitorDailyRollup.error_count), 0).label("error_count"),
                func.coalesce(func.sum(ProviderChannelMonitorDailyRollup.sum_latency_ms), 0).label("sum_latency_ms"),
                func.coalesce(func.sum(ProviderChannelMonitorDailyRollup.count_latency), 0).label("count_latency"),
                func.coalesce(func.sum(ProviderChannelMonitorDailyRollup.sum_ping_latency_ms), 0).label("sum_ping_latency_ms"),
                func.coalesce(func.sum(ProviderChannelMonitorDailyRollup.count_ping_latency), 0).label("count_ping_latency"),
            )
            .where(ProviderChannelMonitorDailyRollup.bucket_date >= since)
            .group_by(ProviderChannelMonitorDailyRollup.monitor_id, ProviderChannelMonitorDailyRollup.model)
        )
    ).all()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        total = int(row.total_checks or 0)
        ok = int(row.operational_count or 0) + int(row.degraded_count or 0)
        count_latency = int(row.count_latency or 0)
        count_ping = int(row.count_ping_latency or 0)
        result[f"{row.monitor_id}:{row.model}"] = {
            "monitor_id": row.monitor_id,
            "model": row.model,
            "total_checks": total,
            "availability_rate": (ok / total) if total else 0.0,
            "avg_latency_ms": int((int(row.sum_latency_ms or 0) / count_latency)) if count_latency else 0,
            "avg_ping_latency_ms": int((int(row.sum_ping_latency_ms or 0) / count_ping)) if count_ping else 0,
            "operational_count": int(row.operational_count or 0),
            "degraded_count": int(row.degraded_count or 0),
            "failed_count": int(row.failed_count or 0),
            "error_count": int(row.error_count or 0),
        }
    return result
