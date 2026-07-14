from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable

import httpx
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal
from .anthropic_adapter import (
    DEFAULT_ANTHROPIC_VERSION,
    build_anthropic_messages_url,
    ensure_claude_code_upstream_headers,
)
from .gemini_cpa import normalize_openai_base_url
from .models import (
    ModelChannelRoute,
    ProviderChannel,
    ProviderChannelMonitor,
    ProviderChannelMonitorDailyRollup,
    ProviderChannelMonitorHistory,
)
from .security import decrypt_api_key, generate_id


logger = logging.getLogger("coincoin.channel_monitoring")

MONITOR_OK_STATUSES = {"operational", "degraded"}
MONITOR_FAILURE_STATUSES = {"failed", "error"}
AUTO_MONITOR_CREATED_BY = "route-reconciler"


@dataclass(frozen=True)
class ProbeResult:
    model: str
    status: str
    latency_ms: int
    ping_latency_ms: int
    status_code: int
    message: str
    checked_at: datetime


@dataclass(frozen=True)
class RouteMonitorSpec:
    channel_id: str
    channel_name: str
    endpoint: str
    models: tuple[str, ...]


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


def _monitor_claim_lease_seconds(monitor: ProviderChannelMonitor) -> int:
    timeout = max(5, int(monitor.timeout_seconds or settings.provider_channel_monitor_default_timeout))
    return max(60, timeout + 30)


def _monitor_endpoint_for_route(route: ModelChannelRoute, channel: ProviderChannel) -> str | None:
    endpoint = str(getattr(route, "endpoint", "") or "").strip()
    if endpoint in {"responses", "chat/completions"}:
        return endpoint
    if endpoint:
        return None
    if _is_anthropic_compatible_channel(channel):
        return "chat/completions"
    return "responses"


def _route_priority(route: ModelChannelRoute, channel: ProviderChannel) -> int:
    override = getattr(route, "priority_override", None)
    return int(override if override is not None else getattr(channel, "priority", 0) or 0)


def _route_weight(route: ModelChannelRoute, channel: ProviderChannel) -> int:
    override = getattr(route, "weight_override", None)
    return max(1, int(override if override is not None else getattr(channel, "weight", 1) or 1))


def _route_monitor_candidates(
    channels: Iterable[ProviderChannel],
    routes: Iterable[ModelChannelRoute],
) -> dict[str, list[tuple[tuple[int, int, str], RouteMonitorSpec]]]:
    channel_by_id = {
        str(channel.id): channel
        for channel in channels
        if str(getattr(channel, "status", "") or "").strip().lower() == "active"
    }
    candidates: dict[str, list[tuple[tuple[int, int, str], RouteMonitorSpec]]] = {}
    for route in routes:
        if str(getattr(route, "status", "") or "").strip().lower() != "active":
            continue
        channel_id = str(getattr(route, "channel_id", "") or "")
        channel = channel_by_id.get(channel_id)
        if channel is None:
            continue
        endpoint = _monitor_endpoint_for_route(route, channel)
        if endpoint is None:
            continue
        model = str(getattr(route, "upstream_model", "") or "").strip()
        if not model:
            continue
        route_id = str(getattr(route, "id", "") or "")
        spec = RouteMonitorSpec(
            channel_id=channel_id,
            channel_name=str(getattr(channel, "name", "") or channel_id),
            endpoint=endpoint,
            models=(model,),
        )
        candidates.setdefault(channel_id, []).append(
            ((_route_priority(route, channel), -_route_weight(route, channel), route_id), spec)
        )
    return candidates


def desired_route_monitor_specs(
    channels: Iterable[ProviderChannel],
    routes: Iterable[ModelChannelRoute],
) -> list[RouteMonitorSpec]:
    candidates = _route_monitor_candidates(channels, routes)
    return [sorted(candidates[channel_id], key=lambda item: item[0])[0][1] for channel_id in sorted(candidates)]


def _auto_monitor_id(channel_id: str) -> str:
    digest = hashlib.sha256(channel_id.encode("utf-8")).hexdigest()[:24]
    return f"cma_{digest}"


def _is_auto_monitor(monitor: ProviderChannelMonitor) -> bool:
    return str(getattr(monitor, "created_by", "") or "") == AUTO_MONITOR_CREATED_BY


async def reconcile_provider_channel_monitors(db: AsyncSession) -> dict[str, int]:
    channels = (await db.execute(select(ProviderChannel))).scalars().all()
    routes = (await db.execute(select(ModelChannelRoute))).scalars().all()
    monitors = (await db.execute(select(ProviderChannelMonitor))).scalars().all()
    specs = desired_route_monitor_specs(channels, routes)
    spec_by_channel = {spec.channel_id: spec for spec in specs}
    active_targets = {
        channel_id: {(spec.endpoint, spec.models[0]) for _sort_key, spec in candidates}
        for channel_id, candidates in _route_monitor_candidates(channels, routes).items()
    }
    monitors_by_channel: dict[str, list[ProviderChannelMonitor]] = {}
    for monitor in monitors:
        monitors_by_channel.setdefault(str(monitor.channel_id), []).append(monitor)

    created = 0
    updated = 0
    disabled = 0
    channel_ids = sorted(set(spec_by_channel) | set(monitors_by_channel))
    for channel_id in channel_ids:
        channel_monitors = monitors_by_channel.get(channel_id, [])
        manual_monitors = sorted(
            (monitor for monitor in channel_monitors if not _is_auto_monitor(monitor)),
            key=lambda monitor: str(getattr(monitor, "id", "") or ""),
        )
        auto_monitors = sorted(
            (monitor for monitor in channel_monitors if _is_auto_monitor(monitor)),
            key=lambda monitor: str(getattr(monitor, "id", "") or ""),
        )

        if manual_monitors:
            valid_targets = active_targets.get(channel_id, set())
            valid_active = [
                monitor
                for monitor in manual_monitors
                if str(getattr(monitor, "status", "") or "").strip().lower() == "active"
                and (
                    str(getattr(monitor, "endpoint", "") or "responses").strip(),
                    str(getattr(monitor, "primary_model", "") or "").strip(),
                )
                in valid_targets
            ]
            keep_manual = valid_active[0] if valid_active else None
            for monitor in manual_monitors:
                changed = False
                if getattr(monitor, "extra_models", None) != serialize_monitor_models([]):
                    monitor.extra_models = serialize_monitor_models([])
                    changed = True
                if monitor is not keep_manual and str(getattr(monitor, "status", "") or "").lower() != "disabled":
                    monitor.status = "disabled"
                    disabled += 1
                if changed:
                    updated += 1
            for monitor in auto_monitors:
                extra_changed = getattr(monitor, "extra_models", None) != serialize_monitor_models([])
                if extra_changed:
                    monitor.extra_models = serialize_monitor_models([])
                if str(getattr(monitor, "status", "") or "").lower() != "disabled":
                    monitor.status = "disabled"
                    disabled += 1
                elif extra_changed:
                    updated += 1
            continue

        spec = spec_by_channel.get(channel_id)
        if spec is None:
            for monitor in auto_monitors:
                extra_changed = getattr(monitor, "extra_models", None) != serialize_monitor_models([])
                if extra_changed:
                    monitor.extra_models = serialize_monitor_models([])
                if str(getattr(monitor, "status", "") or "").lower() != "disabled":
                    monitor.status = "disabled"
                    disabled += 1
                elif extra_changed:
                    updated += 1
            continue

        primary_model = spec.models[0]
        matching_auto = [
            monitor
            for monitor in auto_monitors
            if str(getattr(monitor, "endpoint", "") or "responses").strip() == spec.endpoint
            and str(getattr(monitor, "primary_model", "") or "").strip() == primary_model
        ]
        monitor = matching_auto[0] if matching_auto else (auto_monitors[0] if auto_monitors else None)
        name = f"Auto · {spec.channel_name} · {spec.endpoint}"[:128]
        if monitor is None:
            monitor = ProviderChannelMonitor(
                id=_auto_monitor_id(spec.channel_id),
                channel_id=spec.channel_id,
                name=name,
                endpoint=spec.endpoint,
                primary_model=primary_model,
                extra_models=serialize_monitor_models([]),
                status="active",
                interval_seconds=int(settings.provider_channel_monitor_default_interval),
                timeout_seconds=int(settings.provider_channel_monitor_default_timeout),
                created_by=AUTO_MONITOR_CREATED_BY,
            )
            db.add(monitor)
            created += 1
            continue

        changed = False
        desired_values = {
            "channel_id": spec.channel_id,
            "name": name,
            "endpoint": spec.endpoint,
            "primary_model": primary_model,
            "extra_models": serialize_monitor_models([]),
            "status": "active",
            "interval_seconds": int(settings.provider_channel_monitor_default_interval),
            "timeout_seconds": int(settings.provider_channel_monitor_default_timeout),
            "created_by": AUTO_MONITOR_CREATED_BY,
        }
        for field, value in desired_values.items():
            if getattr(monitor, field, None) != value:
                setattr(monitor, field, value)
                changed = True
        if changed:
            updated += 1
        for redundant in auto_monitors:
            if redundant is monitor:
                continue
            extra_changed = getattr(redundant, "extra_models", None) != serialize_monitor_models([])
            if extra_changed:
                redundant.extra_models = serialize_monitor_models([])
            if str(getattr(redundant, "status", "") or "").strip().lower() != "disabled":
                redundant.status = "disabled"
                disabled += 1
            elif extra_changed:
                updated += 1

    if created or updated or disabled:
        await db.commit()
    return {"created": created, "updated": updated, "disabled": disabled}


def _mask_message(message: str) -> str:
    return str(message or "").replace("\n", " ").strip()[:512]


def _has_structured_model_output(payload: Any, *, endpoint: str, channel_type: str) -> bool:
    if not isinstance(payload, dict):
        return False
    if channel_type == "anthropic_compatible":
        content = payload.get("content")
        return isinstance(content, list) and any(
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and bool(item["text"].strip())
            for item in content
        )
    if endpoint == "chat/completions":
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return False
        for choice in choices:
            message = choice.get("message") if isinstance(choice, dict) else None
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return True
            if isinstance(content, list) and any(
                isinstance(item, dict)
                and isinstance(item.get("text"), str)
                and bool(item["text"].strip())
                for item in content
            ):
                return True
        return False
    output = payload.get("output")
    return isinstance(output, list) and any(
        isinstance(item, dict)
        and item.get("type") == "message"
        and isinstance(item.get("content"), list)
        and any(
            isinstance(content, dict)
            and content.get("type") in {"output_text", "text"}
            and isinstance(content.get("text"), str)
            and bool(content["text"].strip())
            for content in item["content"]
        )
        for item in output
    )


def _status_for_response(
    response: httpx.Response,
    payload: Any,
    latency_ms: int,
    *,
    endpoint: str,
    channel_type: str,
) -> tuple[str, str]:
    if response.status_code in {408, 409, 429} or response.status_code >= 500:
        return "failed", f"HTTP {response.status_code}"
    if response.status_code >= 400:
        return "error", f"HTTP {response.status_code}"
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        return "failed", _mask_message(message or f"HTTP {response.status_code}")
    if not _has_structured_model_output(payload, endpoint=endpoint, channel_type=channel_type):
        return "failed", "response missing structured model output"
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
            ensure_claude_code_upstream_headers(headers, channel)
            headers["x-claude-code-session-id"] = "coincoin-monitor"
            headers["x-stainless-timeout"] = str(settings.provider_channel_monitor_default_timeout)
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


async def _probe_model(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    endpoint: str,
    model: str,
    ping_latency_ms: int,
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
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "stream": False,
        }
    elif endpoint == "chat/completions":
        url = f"{base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "stream": False,
        }
    else:
        url = f"{base_url}/responses"
        payload = {
            "model": model,
            "input": "Reply with OK.",
            "max_output_tokens": 16,
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
        status, message = _status_for_response(
            response,
            data,
            latency_ms,
            endpoint=endpoint,
            channel_type=channel_type,
        )
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

    model = str(getattr(monitor, "primary_model", "") or "").strip()
    if not model:
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
        results = [
            await _probe_model(
                client,
                base_url=base_url,
                headers=headers,
                channel_type=channel_type,
                endpoint=monitor.endpoint,
                model=model,
                ping_latency_ms=0,
            )
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
    monitor.claimed_until = None


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


async def claim_due_provider_channel_monitor_ids(db: AsyncSession, *, limit: int = 10) -> list[str]:
    now = _utcnow()
    rows = (
        await db.execute(
            select(ProviderChannelMonitor)
            .where(
                ProviderChannelMonitor.status == "active",
                or_(
                    ProviderChannelMonitor.claimed_until.is_(None),
                    ProviderChannelMonitor.claimed_until <= now,
                ),
            )
            .order_by(
                case((ProviderChannelMonitor.last_checked_at.is_(None), 0), else_=1).asc(),
                ProviderChannelMonitor.last_checked_at.asc(),
                ProviderChannelMonitor.created_at.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(max(1, int(limit or 10)))
        )
    ).scalars().all()
    due: list[str] = []
    for row in rows:
        claimed_until = getattr(row, "claimed_until", None)
        if claimed_until is not None and claimed_until > now:
            continue
        last_checked = row.last_checked_at
        interval = max(15, int(row.interval_seconds or settings.provider_channel_monitor_default_interval))
        if last_checked is None or last_checked <= now - timedelta(seconds=interval):
            row.claimed_until = now + timedelta(seconds=_monitor_claim_lease_seconds(row))
            due.append(row.id)
    await db.commit()
    return due


async def provider_channel_monitor_loop(poll_interval_seconds: int) -> None:
    if not settings.provider_channel_monitor_enabled:
        logger.info("provider channel monitor loop disabled")
        return
    await asyncio.sleep(2)
    last_cleanup_at = 0.0
    last_reconcile_at = 0.0
    while True:
        try:
            async with SessionLocal() as db:
                now_monotonic = time.monotonic()
                if now_monotonic - last_reconcile_at >= 60:
                    changes = await reconcile_provider_channel_monitors(db)
                    if any(changes.values()):
                        logger.info("provider channel monitor reconcile changes=%s", changes)
                    last_reconcile_at = now_monotonic
                for _ in range(10):
                    monitor_ids = await claim_due_provider_channel_monitor_ids(db, limit=1)
                    if not monitor_ids:
                        break
                    monitor_id = monitor_ids[0]
                    try:
                        monitor = await db.get(ProviderChannelMonitor, monitor_id)
                        if monitor is None:
                            continue
                        hard_timeout = max(5, _monitor_claim_lease_seconds(monitor) - 5)
                        await asyncio.wait_for(
                            run_provider_channel_monitor_once(db, monitor_id),
                            timeout=hard_timeout,
                        )
                    except Exception as exc:
                        await db.rollback()
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
