from __future__ import annotations

import random
import time
import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


ACTIVE_STATUS = "active"


@dataclass(frozen=True)
class ProviderChannelSnapshot:
    channel_id: str
    name: str = ""
    provider_platform: str = ""
    channel_type: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    auth_style: str = "bearer"
    status: str = ACTIVE_STATUS
    priority: int = 0
    weight: int = 1
    allowed_fails: int = 3
    cooldown_seconds: float = 30.0
    capabilities: Tuple[str, ...] = ()
    provider_account_fingerprint: str = ""
    cost_tier: str = ""
    notes: str = ""
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class ModelChannelRouteSnapshot:
    route_id: str
    public_model_id: str
    endpoint: str = ""
    channel_id: str = ""
    upstream_model: str = ""
    priority_override: Optional[int] = None
    weight_override: Optional[int] = None
    transform_profile: str = "openai_compatible"
    status: str = ACTIVE_STATUS
    notes: str = ""
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class ChannelChoice:
    route_id: str
    channel_id: str
    provider_model: str
    upstream_url: str
    api_key: str
    auth_style: str
    priority: int
    weight: int
    channel_type: str
    provider_platform: str
    provider_account_fingerprint: str = ""
    transform_profile: str = "openai_compatible"
    cost_tier: str = ""
    route_attempt: int = 0
    allowed_fails: int = 3
    cooldown_seconds: float = 30.0


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _split_csv_or_jsonish(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple, set)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    text = str(raw or "").strip()
    if not text:
        return ()
    if text.startswith("[") and text.endswith("]"):
        try:
            import json

            loaded = json.loads(text)
            if isinstance(loaded, list):
                return tuple(str(item).strip() for item in loaded if str(item).strip())
        except Exception:
            pass
    return tuple(item.strip() for item in text.replace("\n", ",").split(",") if item.strip())


class ChannelRouter:
    def __init__(self) -> None:
        self._channels: Dict[str, ProviderChannelSnapshot] = {}
        self._routes_by_model: Dict[str, List[ModelChannelRouteSnapshot]] = {}
        self._version: int = 0
        self._state: Dict[str, Dict[str, Any]] = {}

    def set_snapshot(
        self,
        channels: Iterable[ProviderChannelSnapshot],
        routes: Iterable[ModelChannelRouteSnapshot],
        *,
        version: int = 0,
    ) -> None:
        self._channels = {item.channel_id: item for item in channels if item.channel_id}
        routes_by_model: Dict[str, List[ModelChannelRouteSnapshot]] = {}
        for route in routes:
            if not route.public_model_id or not route.channel_id:
                continue
            routes_by_model.setdefault(route.public_model_id, []).append(route)
        self._routes_by_model = routes_by_model
        self._version = int(version or 0)

    def clear_snapshot(self) -> None:
        self._channels = {}
        self._routes_by_model = {}
        self._version = 0
        self._state = {}

    @property
    def version(self) -> int:
        return self._version

    def list_channels(self) -> List[ProviderChannelSnapshot]:
        return sorted(
            self._channels.values(),
            key=lambda item: (item.priority, item.provider_platform, item.name, item.channel_id),
        )

    def list_routes(self, public_model_id: str = "") -> List[ModelChannelRouteSnapshot]:
        if public_model_id:
            return list(self._routes_by_model.get(public_model_id, ()))
        result: List[ModelChannelRouteSnapshot] = []
        for routes in self._routes_by_model.values():
            result.extend(routes)
        return sorted(result, key=lambda item: (item.public_model_id, item.endpoint, item.route_id))

    def has_routes_for_model(self, public_model_id: str) -> bool:
        return bool(self._routes_by_model.get(public_model_id))

    def _cooldown_until(self, channel_id: str) -> float:
        return _as_float((self._state.get(channel_id) or {}).get("cooldown_until"), 0.0)

    def _is_available(self, channel: ProviderChannelSnapshot, now: float) -> bool:
        if (channel.status or "").strip().lower() != ACTIVE_STATUS:
            return False
        return self._cooldown_until(channel.channel_id) <= now

    def select_for_model(
        self,
        public_model: Any,
        backend: Any,
        endpoint: str,
        *,
        exclude_channel_ids: Iterable[str] = (),
        affinity_key: str = "",
    ) -> Optional[ChannelChoice]:
        public_id = str(getattr(public_model, "public_id", "") or "").strip()
        if not public_id:
            return None
        routes = self._routes_by_model.get(public_id) or []
        if not routes:
            return None

        now = time.time()
        endpoint = str(endpoint or "").strip()
        excluded = {str(item or "").strip() for item in exclude_channel_ids if str(item or "").strip()}
        candidates: List[Tuple[ModelChannelRouteSnapshot, ProviderChannelSnapshot, int, int]] = []
        for route in routes:
            if (route.status or "").strip().lower() != ACTIVE_STATUS:
                continue
            route_endpoint = str(route.endpoint or "").strip()
            if route_endpoint and route_endpoint != endpoint:
                continue
            channel = self._channels.get(route.channel_id)
            if channel is not None and channel.channel_id in excluded:
                continue
            if channel is None or not self._is_available(channel, now):
                continue
            if channel.capabilities and endpoint not in channel.capabilities:
                continue
            if not (channel.base_url and channel.api_key):
                continue
            priority = route.priority_override if route.priority_override is not None else channel.priority
            weight = route.weight_override if route.weight_override is not None else channel.weight
            candidates.append((route, channel, int(priority or 0), max(1, int(weight or 1))))

        if not candidates:
            return None

        best_priority = min(item[2] for item in candidates)
        tier = [item for item in candidates if item[2] == best_priority]
        affinity = str(affinity_key or "").strip()
        if affinity:
            selected = tier[-1]
            best_score = float("inf")
            for item in tier:
                route, channel, _priority, weight = item
                seed = f"{affinity}:{public_id}:{endpoint}:{route.route_id}:{channel.channel_id}"
                digest = hashlib.sha256(seed.encode("utf-8")).digest()
                bucket = int.from_bytes(digest[:8], "big")
                unit = (bucket + 1) / ((1 << 64) + 1)
                score = -math.log(unit) / max(1, int(weight or 1))
                if score < best_score:
                    best_score = score
                    selected = item
        else:
            total_weight = sum(item[3] for item in tier)
            cursor = random.uniform(0, total_weight)
            upto = 0.0
            selected = tier[-1]
            for item in tier:
                upto += item[3]
                if cursor <= upto:
                    selected = item
                    break

        route, channel, priority, weight = selected
        provider_model = str(
            route.upstream_model
            or getattr(backend, "model_id", "")
            or getattr(public_model, "upstream_model", "")
            or getattr(public_model, "provider_model", "")
            or public_id
        ).strip()
        return ChannelChoice(
            route_id=route.route_id,
            channel_id=channel.channel_id,
            provider_model=provider_model,
            upstream_url=channel.base_url,
            api_key=channel.api_key,
            auth_style=channel.auth_style or getattr(backend, "auth_style", "bearer") or "bearer",
            priority=priority,
            weight=weight,
            channel_type=channel.channel_type,
            provider_platform=channel.provider_platform,
            provider_account_fingerprint=channel.provider_account_fingerprint,
            transform_profile=route.transform_profile or "openai_compatible",
            cost_tier=channel.cost_tier,
            route_attempt=0,
            allowed_fails=channel.allowed_fails,
            cooldown_seconds=channel.cooldown_seconds,
        )

    def record_success(self, channel_id: str, *, latency_ms: int = 0) -> None:
        if not channel_id:
            return
        state = self._state.setdefault(channel_id, {})
        state["failures"] = 0
        state["cooldown_until"] = 0.0
        state["last_success_at"] = time.time()
        if latency_ms > 0:
            state["rolling_latency_ms"] = latency_ms

    def record_failure(self, channel_id: str, *, error_code: str = "") -> None:
        if not channel_id:
            return
        channel = self._channels.get(channel_id)
        if channel is None:
            return
        state = self._state.setdefault(channel_id, {"failures": 0, "cooldown_until": 0.0})
        failures = int(state.get("failures") or 0) + 1
        state["failures"] = failures
        state["last_failure_at"] = time.time()
        state["last_error_code"] = (error_code or "")[:64]
        if failures >= max(1, channel.allowed_fails):
            state["cooldown_until"] = time.time() + max(0.0, channel.cooldown_seconds)
            state["failures"] = 0

    def reset_channel_state(self, channel_id: str) -> None:
        if channel_id:
            self._state.pop(channel_id, None)

    def channel_state(self, channel_id: str) -> Dict[str, Any]:
        return dict(self._state.get(channel_id) or {})


def should_record_failure(status_code: int) -> bool:
    return status_code in {408, 409, 429} or status_code >= 500


channel_router = ChannelRouter()
