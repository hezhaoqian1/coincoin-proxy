from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .channel_router import channel_router
from .config import settings


logger = logging.getLogger("coincoin.router")


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    upstream_url: str
    api_key: str
    price_input_per_million: int
    price_output_per_million: int
    strip_unsupported: bool
    auth_style: str = "azure"  # "azure" → api-key header; "bearer" → Authorization: Bearer
    channel_id: str = ""
    route_id: str = ""
    channel_type: str = ""
    provider_platform: str = ""
    provider_account_fingerprint: str = ""
    transform_profile: str = ""
    cost_tier: str = ""
    fallback_from_channel_id: str = ""
    route_attempt: int = 0
    channel_priority: int = 0
    channel_weight: int = 1
    allowed_fails: int = 3
    cooldown_seconds: float = 30.0


@dataclass(frozen=True)
class PublicModelConfig:
    public_id: str
    owned_by: str = "coincoin"
    provider_name: str = ""
    capabilities: Tuple[str, ...] = ()
    routing_mode: str = "direct"  # direct | legacy_auto | route_only
    delivery_lane: str = "upstream_direct"  # legacy | gateway | cpa_gemini | vertex_direct | upstream_direct | route_only
    upstream_model: str = ""
    provider_model: str = ""
    upstream_url: str = ""
    api_key: str = ""
    auth_style: str = "bearer"
    base_price_input_per_million: int = 0
    base_price_output_per_million: int = 0
    base_price_per_image_cents: float = 0.0
    base_price_per_video_cents: float = 0.0
    price_input_per_million: int = 0
    price_output_per_million: int = 0
    price_per_image_cents: float = 0.0
    price_per_video_cents: float = 0.0
    effective_cached_input_per_million: float = 0.0
    pricing_mode: str = "explicit_price"
    model_multiplier: float = 1.0
    output_multiplier: float = 1.0
    cache_read_multiplier: float = 0.0
    image_multiplier: float = 1.0
    video_multiplier: float = 1.0
    price_version: int = 0
    billable_sku: str = ""
    created: int = 1700000000
    strip_unsupported: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedModel:
    public_model: PublicModelConfig
    backend: ModelConfig
    execution_profile: str
    execution_pool: str
    route_reason: str
    lock_model_selection: bool = False


@dataclass(frozen=True)
class ExecutionProfile:
    profile_id: str
    pool_id: str
    legacy_default_slot: Optional[str] = None
    honor_tool_routing: bool = True


class ModelResolutionError(ValueError):
    pass


class UnknownModelError(ModelResolutionError):
    pass


class ModelCapabilityError(ModelResolutionError):
    pass


PREMIUM = "premium"
CHEAP = "cheap"
FALLBACK = "fallback"
EMBEDDING = "embedding"
LEGACY_ROUTE_SLOTS = frozenset({PREMIUM, CHEAP, FALLBACK, EMBEDDING})

TEXT_ENDPOINTS = frozenset({"chat/completions", "responses"})
EMBEDDING_ENDPOINTS = frozenset({"embeddings"})
IMAGE_ENDPOINTS = frozenset({"images/generations", "images/edits"})
VIDEO_ENDPOINTS = frozenset({"videos/generations"})
DELIVERY_LANES = frozenset({"legacy", "gateway", "cpa_gemini", "vertex_direct", "upstream_direct", "kiro_go", "route_only"})
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(:-([^}]*))?\}")
_ROOT_DIR = Path(__file__).resolve().parent.parent
ALIAS_OVERRIDE_FIELDS = frozenset({"provider_model", "upstream_model", "enabled"})
LEGACY_PROVIDER_MODEL_ALIASES = {
    # CPA no longer publishes this historical public alias directly.
    # Keep the user-facing model id stable, but send a provider model CPA knows.
    "gpt-5.2-codex": "gpt-5.3-codex",
}
LEGACY_CODING_PUBLIC_ALIASES = frozenset({"gpt-5.2-codex", "gpt-5.3-codex", "gpt-5.3-codex-spark"})
CLAUDE_COMPAT_FAMILY = "claude-code"
CLAUDE_COMPAT_PROVIDER_UPSTREAM_DIRECT = "upstream_direct"
CLAUDE_COMPAT_PROVIDER_KIRO_GO = "kiro_go"
CLAUDE_COMPAT_PROVIDERS = frozenset({
    CLAUDE_COMPAT_PROVIDER_UPSTREAM_DIRECT,
    CLAUDE_COMPAT_PROVIDER_KIRO_GO,
})
CLAUDE_COMPAT_KIRO_MODEL_MAP = {
    "claude-opus-4-7": "claude-opus-4.7",
    "claude-opus-4.7": "claude-opus-4.7",
    "claude-opus-4.6": "claude-opus-4.6",
    "claude-opus-4.5": "claude-opus-4.5",
    "opus": "claude-opus-4.7",
    "best": "claude-opus-4.7",
    "default": "claude-opus-4.7",
    "opus[1m]": "claude-opus-4.7",
    "opusplan": "claude-opus-4.7",
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-sonnet-4.6": "claude-sonnet-4.6",
    "claude-sonnet-4.5": "claude-sonnet-4.5",
    "claude-sonnet-4": "claude-sonnet-4",
    "sonnet": "claude-sonnet-4.6",
    "sonnet[1m]": "claude-sonnet-4.6",
    "claude-haiku-4-5": "claude-haiku-4.5",
    "claude-haiku-4.5": "claude-haiku-4.5",
    "claude-haiku-4-5-20251001": "claude-haiku-4.5",
    "haiku": "claude-haiku-4.5",
}


def _is_codex_like(model_id: str) -> bool:
    return "codex" in (model_id or "").lower()


def _provider_model_for_legacy_alias(model_id: str) -> str:
    normalized = (model_id or "").strip()
    return LEGACY_PROVIDER_MODEL_ALIASES.get(normalized, normalized)


def _normalized_claude_compat_provider(value: Any) -> str:
    provider = str(value or CLAUDE_COMPAT_PROVIDER_UPSTREAM_DIRECT).strip().lower()
    if provider not in CLAUDE_COMPAT_PROVIDERS:
        return CLAUDE_COMPAT_PROVIDER_UPSTREAM_DIRECT
    return provider


def _is_claude_compat_model(metadata: Dict[str, Any]) -> bool:
    return str((metadata or {}).get("compat_family") or "").strip().lower() == CLAUDE_COMPAT_FAMILY


def _kiro_go_claude_model_for_public_id(public_id: str, upstream_model: str, provider_model: str) -> str:
    normalized = str(public_id or "").strip().lower()
    mapped = CLAUDE_COMPAT_KIRO_MODEL_MAP.get(normalized)
    if mapped:
        return mapped
    upstream_clean = str(upstream_model or "").strip()
    if upstream_clean:
        return upstream_clean
    return str(provider_model or public_id).strip()


def _default_legacy_metadata(public_id: str) -> Dict[str, Any]:
    if public_id in LEGACY_CODING_PUBLIC_ALIASES:
        return {
            "execution_profile": "legacy_coding",
            "execution_pool": "cpa_coding_pool",
            "legacy_default_slot": PREMIUM,
            "honor_tool_routing": False,
        }
    return {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _non_negative_float(value: Any, default: float = 1.0) -> float:
    parsed = _as_float(value, default)
    return parsed if parsed >= 0 else default


def _lookup_placeholder(name: str, default: str = "") -> str:
    env_value = os.getenv(name)
    if env_value not in (None, ""):
        return env_value

    if name.startswith("COINCOIN_"):
        attr_name = name[len("COINCOIN_"):].lower()
        if hasattr(settings, attr_name):
            value = getattr(settings, attr_name)
            if value not in (None, ""):
                return str(value)
    return default


def _split_placeholder_expr(expr: str) -> Tuple[str, str]:
    depth = 0
    i = 0
    while i < len(expr) - 1:
        if expr.startswith("${", i):
            depth += 1
            i += 2
            continue
        if expr[i] == "}" and depth > 0:
            depth -= 1
            i += 1
            continue
        if depth == 0 and expr.startswith(":-", i):
            return expr[:i], expr[i + 2:]
        i += 1
    return expr, ""


def _resolve_placeholder_string(value: str) -> str:
    if "${" not in value:
        return value

    parts: List[str] = []
    i = 0
    while i < len(value):
        if not value.startswith("${", i):
            parts.append(value[i])
            i += 1
            continue

        depth = 1
        j = i + 2
        while j < len(value) and depth > 0:
            if value.startswith("${", j):
                depth += 1
                j += 2
                continue
            if value[j] == "}":
                depth -= 1
                j += 1
                continue
            j += 1

        if depth != 0:
            parts.append(value[i:])
            break

        expr = value[i + 2:j - 1]
        name, default = _split_placeholder_expr(expr)
        resolved = _lookup_placeholder(name.strip(), "")
        if resolved in ("", None):
            resolved = _resolve_placeholder_string(default) if default else ""
        parts.append(str(resolved))
        i = j

    return "".join(parts)


def _resolve_placeholders(value: Any) -> Any:
    if isinstance(value, str):
        return _resolve_placeholder_string(value)
    if isinstance(value, list):
        return [_resolve_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_placeholders(item) for key, item in value.items()}
    return value


def _catalog_path(raw_path: str) -> Path:
    path = Path((raw_path or "").strip())
    if not path.is_absolute():
        path = _ROOT_DIR / path
    return path


def _load_json_file(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except Exception as exc:
        logger.warning("failed to load json file %s: %s", path, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _safe_alias_overrides(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    aliases = raw.get("aliases") if isinstance(raw, dict) else None
    if not isinstance(aliases, dict):
        return {}

    safe: Dict[str, Dict[str, Any]] = {}
    for alias_id, fields in aliases.items():
        alias = str(alias_id or "").strip()
        if not alias or not isinstance(fields, dict):
            continue
        filtered: Dict[str, Any] = {}
        for field_name in ALIAS_OVERRIDE_FIELDS:
            if field_name not in fields:
                continue
            value = fields[field_name]
            if field_name == "enabled":
                filtered[field_name] = bool(value) if isinstance(value, bool) else str(value).strip().lower()
            else:
                filtered[field_name] = str(value or "").strip()
        if filtered:
            safe[alias] = filtered
    return safe


class ModelRegistry:
    def __init__(self) -> None:
        self.models: Dict[str, ModelConfig] = {}
        self.public_models: Dict[str, PublicModelConfig] = {}
        self.public_model_order: List[str] = []
        self.router_enabled: bool = False
        self.tool_count_threshold: int = 2
        self.default_text_model_id: str = ""
        self.default_embedding_model_id: str = ""
        self.default_image_model_id: str = ""
        self.default_video_model_id: str = ""
        self.alias_overrides: Dict[str, Dict[str, Any]] = {}
        self._runtime_alias_overrides: Optional[Dict[str, Dict[str, Any]]] = None
        self._runtime_alias_override_version: int = 0
        self._raw_public_models: Dict[str, Dict[str, Any]] = {}
        self._alias_override_state: Tuple[str, int] = ("", -1)
        self.pricing_overrides: Dict[str, Dict[str, Any]] = {}
        self._runtime_pricing_overrides: Optional[Dict[str, Dict[str, Any]]] = None
        self._runtime_pricing_override_version: int = 0
        self._pricing_override_state: Tuple[str, int] = ("", -1)
        self._runtime_system_settings: Optional[Dict[str, Any]] = None
        self._runtime_system_settings_version: int = 0
        self._system_settings_state_snapshot: Tuple[str, int] = ("env", 0)
        self._initialized: bool = False

    def init_from_settings(self) -> None:
        # Idempotent init; safe to call multiple times.
        self.router_enabled = bool(getattr(settings, "router_enabled", False))
        self.tool_count_threshold = int(getattr(settings, "router_tool_count_threshold", 2) or 2)
        self._init_legacy_backends()
        self._init_public_catalog()
        self._alias_override_state = self._current_alias_override_state()
        self._pricing_override_state = self._current_pricing_override_state()
        self._system_settings_state_snapshot = self._current_system_settings_state()
        self._initialized = True

    def _init_legacy_backends(self) -> None:
        primary_strip = bool(
            getattr(settings, "primary_strip_unsupported", False)
        ) or _is_codex_like(settings.fixed_model)
        embedding_upstream_url = (
            getattr(settings, "embedding_upstream_url", "") or
            getattr(settings, "fallback_upstream_url", "") or
            settings.upstream_base_url
        )
        embedding_api_key = (
            getattr(settings, "embedding_api_key", "") or
            getattr(settings, "fallback_api_key", "") or
            settings.upstream_api_key
        )
        embedding_auth_style = (
            getattr(settings, "embedding_auth_style", "") or
            getattr(settings, "fallback_auth_style", "") or
            settings.primary_auth_style
        )
        embedding_price_input = int(
            getattr(settings, "embedding_price_input", 0) or
            getattr(settings, "fallback_price_input", 0) or
            settings.price_input_per_million
        )

        premium = ModelConfig(
            model_id=_provider_model_for_legacy_alias(settings.fixed_model),
            upstream_url=settings.upstream_base_url,
            api_key=settings.upstream_api_key,
            price_input_per_million=settings.price_input_per_million,
            price_output_per_million=settings.price_output_per_million,
            strip_unsupported=primary_strip,
            auth_style=settings.primary_auth_style,
        )
        self.models = {PREMIUM: premium}

        self.models[EMBEDDING] = ModelConfig(
            model_id=getattr(settings, "embedding_model", "text-embedding-3-small"),
            upstream_url=embedding_upstream_url,
            api_key=embedding_api_key,
            price_input_per_million=embedding_price_input,
            price_output_per_million=0,
            strip_unsupported=False,
            auth_style=embedding_auth_style,
        )

        cheap_model = (getattr(settings, "cheap_model", "") or "").strip()
        if self.router_enabled and cheap_model:
            self.models[CHEAP] = ModelConfig(
                model_id=_provider_model_for_legacy_alias(cheap_model),
                upstream_url=(getattr(settings, "cheap_upstream_url", "") or settings.upstream_base_url),
                api_key=(getattr(settings, "cheap_api_key", "") or settings.upstream_api_key),
                price_input_per_million=int(getattr(settings, "cheap_price_input", 0) or 0),
                price_output_per_million=int(getattr(settings, "cheap_price_output", 0) or 0),
                strip_unsupported=_is_codex_like(cheap_model),
                auth_style=settings.primary_auth_style,
            )

        fallback_model = (getattr(settings, "fallback_model", "") or "").strip()
        if fallback_model:
            fb_auth = (getattr(settings, "fallback_auth_style", "") or "").strip()
            self.models[FALLBACK] = ModelConfig(
                model_id=_provider_model_for_legacy_alias(fallback_model),
                upstream_url=(getattr(settings, "fallback_upstream_url", "") or settings.upstream_base_url),
                api_key=(getattr(settings, "fallback_api_key", "") or settings.upstream_api_key),
                price_input_per_million=int(getattr(settings, "fallback_price_input", 0) or 0),
                price_output_per_million=int(getattr(settings, "fallback_price_output", 0) or 0),
                strip_unsupported=_is_codex_like(fallback_model),
                auth_style=fb_auth or settings.primary_auth_style,
            )

    def _default_catalog_document(self) -> Dict[str, Any]:
        return {
            "default_text_model": settings.fixed_model,
            "default_video_model": "",
            "models": [
                {
                    "id": settings.fixed_model,
                    "owned_by": "openai",
                    "provider_name": "OpenAI",
                    "capabilities": ["chat/completions", "responses"],
                    "routing_mode": "legacy_auto",
                    "billable_sku": "legacy-default-text",
                },
                {
                    "id": getattr(settings, "embedding_model", "text-embedding-3-small"),
                    "owned_by": "openai",
                    "provider_name": "OpenAI",
                    "provider_model": getattr(settings, "embedding_model", "text-embedding-3-small"),
                    "capabilities": ["embeddings"],
                    "routing_mode": "direct",
                    "delivery_lane": "upstream_direct",
                    "upstream_model": getattr(settings, "embedding_model", "text-embedding-3-small"),
                    "upstream_url": (
                        getattr(settings, "embedding_upstream_url", "") or
                        getattr(settings, "fallback_upstream_url", "") or
                        settings.upstream_base_url
                    ),
                    "api_key": (
                        getattr(settings, "embedding_api_key", "") or
                        getattr(settings, "fallback_api_key", "") or
                        settings.upstream_api_key
                    ),
                    "auth_style": (
                        getattr(settings, "embedding_auth_style", "") or
                        getattr(settings, "fallback_auth_style", "") or
                        settings.primary_auth_style
                    ),
                    "price_input_per_million": int(
                        getattr(settings, "embedding_price_input", 0) or
                        getattr(settings, "fallback_price_input", 0) or
                        settings.price_input_per_million
                    ),
                    "price_output_per_million": 0,
                    "billable_sku": "azure-text-embedding-3-small",
                    "metadata": {"tier": "stable"},
                },
            ],
        }

    def _load_catalog_document(self) -> Dict[str, Any]:
        raw_json = (getattr(settings, "model_catalog_json", "") or "").strip()
        if raw_json:
            try:
                loaded = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                logger.warning("invalid COINCOIN_MODEL_CATALOG_JSON: %s", exc)
                return self._default_catalog_document()
            return _resolve_placeholders(loaded)

        raw_path = (getattr(settings, "model_catalog_path", "") or "config/model_catalog.json").strip()
        catalog_path = _catalog_path(raw_path)
        if not catalog_path.is_file():
            logger.info("model catalog not found at %s; using default legacy-only catalog", catalog_path)
            return self._default_catalog_document()

        try:
            loaded = json.loads(catalog_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to load model catalog %s: %s", catalog_path, exc)
            return self._default_catalog_document()
        return _resolve_placeholders(loaded)

    def _load_alias_overrides(self) -> Dict[str, Dict[str, Any]]:
        if self._runtime_alias_overrides is not None:
            return dict(self._runtime_alias_overrides)
        raw_path = (getattr(settings, "model_alias_overrides_path", "") or "").strip()
        if not raw_path:
            return {}
        return _safe_alias_overrides(_resolve_placeholders(_load_json_file(_catalog_path(raw_path))))

    def _load_pricing_overrides(self) -> Dict[str, Dict[str, Any]]:
        if self._runtime_pricing_overrides is not None:
            return {key: dict(value or {}) for key, value in self._runtime_pricing_overrides.items()}
        return {}

    def _apply_alias_overrides(self, raw_models: List[Any]) -> List[Any]:
        self.alias_overrides = self._load_alias_overrides()
        if not self.alias_overrides:
            return raw_models

        result: List[Any] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                result.append(raw)
                continue
            public_id = str(raw.get("id") or "").strip()
            override = self.alias_overrides.get(public_id)
            result.append({**raw, **override} if override else raw)
        return result

    def _pricing_for_raw_model(self, public_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
        override = self.pricing_overrides.get(public_id) or {}
        return {**pricing, **override}

    def _compile_prices(self, public_id: str, raw: Dict[str, Any], routing_mode: str) -> Dict[str, Any]:
        default_price_input = settings.price_input_per_million if routing_mode == "legacy_auto" else 0
        default_price_output = settings.price_output_per_million if routing_mode == "legacy_auto" else 0
        base_input = _as_int(raw.get("price_input_per_million"), default_price_input)
        base_output = _as_int(raw.get("price_output_per_million"), default_price_output)
        base_image = _as_float(raw.get("price_per_image_cents"), 0.0)
        base_video = _as_float(raw.get("price_per_video_cents"), 0.0)
        pricing = self._pricing_for_raw_model(public_id, raw)

        model_multiplier = _non_negative_float(pricing.get("model_multiplier"), 1.0)
        output_multiplier = _non_negative_float(pricing.get("output_multiplier"), 1.0)
        cache_default = _as_float(getattr(settings, "cache_discount_rate", 0.0), 0.0)
        cache_read_multiplier = _non_negative_float(pricing.get("cache_read_multiplier"), cache_default)
        image_multiplier = _non_negative_float(pricing.get("image_multiplier"), 1.0)
        video_multiplier = _non_negative_float(pricing.get("video_multiplier"), 1.0)
        has_multiplier = any(
            key in pricing
            for key in ("model_multiplier", "output_multiplier", "cache_read_multiplier", "image_multiplier", "video_multiplier")
        )
        pricing_mode = str(pricing.get("pricing_mode") or ("multiplier" if has_multiplier else "explicit_price")).strip() or "explicit_price"

        effective_input = round(base_input * model_multiplier)
        effective_output = round(base_output * model_multiplier * output_multiplier)
        effective_image = base_image * image_multiplier
        effective_video = base_video * video_multiplier
        effective_cached = round(effective_input * cache_read_multiplier, 4)

        return {
            "base_price_input_per_million": base_input,
            "base_price_output_per_million": base_output,
            "base_price_per_image_cents": base_image,
            "base_price_per_video_cents": base_video,
            "price_input_per_million": effective_input,
            "price_output_per_million": effective_output,
            "price_per_image_cents": effective_image,
            "price_per_video_cents": effective_video,
            "effective_cached_input_per_million": effective_cached,
            "pricing_mode": pricing_mode,
            "model_multiplier": model_multiplier,
            "output_multiplier": output_multiplier,
            "cache_read_multiplier": cache_read_multiplier,
            "image_multiplier": image_multiplier,
            "video_multiplier": video_multiplier,
            "price_version": _as_int(pricing.get("price_version"), 0),
        }

    def _build_public_model(self, raw: Dict[str, Any]) -> Optional[PublicModelConfig]:
        public_id = str(raw.get("id") or "").strip()
        if not public_id:
            return None

        capabilities = tuple(
            cap for cap in (
                str(item).strip() for item in (raw.get("capabilities") or [])
            )
            if cap
        )
        routing_mode = str(raw.get("routing_mode") or "direct").strip().lower()
        default_delivery_lane = "legacy" if routing_mode == "legacy_auto" else ("route_only" if routing_mode == "route_only" else "upstream_direct")
        delivery_lane = str(raw.get("delivery_lane") or default_delivery_lane).strip().lower()
        if delivery_lane not in DELIVERY_LANES:
            logger.warning(
                "public model %s has unsupported delivery_lane=%r; falling back to %s",
                public_id,
                delivery_lane,
                default_delivery_lane,
            )
            delivery_lane = default_delivery_lane
        provider_name = str(raw.get("provider_name") or "").strip()
        provider_model = str(raw.get("provider_model") or "").strip()
        upstream_model = str(raw.get("upstream_model") or "").strip()
        if routing_mode == "legacy_auto":
            provider_model = _provider_model_for_legacy_alias(provider_model or public_id)
            if upstream_model:
                upstream_model = _provider_model_for_legacy_alias(upstream_model)
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        if routing_mode == "legacy_auto":
            metadata = {**_default_legacy_metadata(public_id), **metadata}
        upstream_url = str(raw.get("upstream_url") or "").strip()
        api_key = str(raw.get("api_key") or "").strip()
        auth_style = str(raw.get("auth_style") or settings.gateway_auth_style or "bearer").strip() or "bearer"
        if _is_claude_compat_model(metadata):
            provider = self.current_claude_compat_provider()
            if provider == CLAUDE_COMPAT_PROVIDER_KIRO_GO:
                upstream_url = str(getattr(settings, "claude_compat_base_url", "") or "").strip()
                api_key = str(getattr(settings, "claude_compat_api_key", "") or "").strip()
                auth_style = str(getattr(settings, "claude_compat_auth_style", "") or "bearer").strip() or "bearer"
                provider_model = _kiro_go_claude_model_for_public_id(public_id, upstream_model, provider_model)
                upstream_model = provider_model
                delivery_lane = CLAUDE_COMPAT_PROVIDER_KIRO_GO
                metadata = {**metadata, "claude_compat_provider": CLAUDE_COMPAT_PROVIDER_KIRO_GO}
            else:
                routing_mode = "route_only"
                delivery_lane = "route_only"
                upstream_url = ""
                api_key = ""
                metadata = {**metadata, "claude_compat_provider": CLAUDE_COMPAT_PROVIDER_UPSTREAM_DIRECT}
        prices = self._compile_prices(public_id, raw, routing_mode)

        return PublicModelConfig(
            public_id=public_id,
            owned_by=str(raw.get("owned_by") or "coincoin").strip() or "coincoin",
            provider_name=provider_name,
            capabilities=capabilities,
            routing_mode=routing_mode,
            delivery_lane=delivery_lane,
            upstream_model=upstream_model,
            provider_model=provider_model,
            upstream_url=upstream_url,
            api_key=api_key,
            auth_style=auth_style,
            base_price_input_per_million=prices["base_price_input_per_million"],
            base_price_output_per_million=prices["base_price_output_per_million"],
            base_price_per_image_cents=prices["base_price_per_image_cents"],
            base_price_per_video_cents=prices["base_price_per_video_cents"],
            price_input_per_million=prices["price_input_per_million"],
            price_output_per_million=prices["price_output_per_million"],
            price_per_image_cents=prices["price_per_image_cents"],
            price_per_video_cents=prices["price_per_video_cents"],
            effective_cached_input_per_million=prices["effective_cached_input_per_million"],
            pricing_mode=prices["pricing_mode"],
            model_multiplier=prices["model_multiplier"],
            output_multiplier=prices["output_multiplier"],
            cache_read_multiplier=prices["cache_read_multiplier"],
            image_multiplier=prices["image_multiplier"],
            video_multiplier=prices["video_multiplier"],
            price_version=prices["price_version"],
            billable_sku=str(raw.get("billable_sku") or public_id).strip() or public_id,
            created=_as_int(raw.get("created"), 1700000000),
            strip_unsupported=_as_bool(raw.get("strip_unsupported"), False),
            metadata=metadata,
        )

    def _init_public_catalog(self) -> None:
        self.public_models = {}
        self.public_model_order = []
        self._raw_public_models = {}

        document = self._load_catalog_document()
        raw_models = document.get("models")
        if not isinstance(raw_models, list):
            raw_models = []
        self.pricing_overrides = self._load_pricing_overrides()
        raw_models = self._apply_alias_overrides(raw_models)

        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            public_id = str(raw.get("id") or "").strip()
            if public_id:
                self._raw_public_models[public_id] = dict(raw)
            enabled = raw.get("enabled")
            if enabled is not None and not _as_bool(enabled, True):
                continue
            model = self._build_public_model(raw)
            if model is None:
                continue
            if model.routing_mode not in {"legacy_auto", "route_only"}:
                if not (model.upstream_model and model.upstream_url and model.api_key):
                    logger.warning(
                        "skipping public model %s because upstream config is incomplete for delivery_lane=%s",
                        model.public_id,
                        model.delivery_lane,
                    )
                    continue
            if model.public_id in self.public_models:
                logger.warning("skipping duplicate public model id %s", model.public_id)
                continue
            self.public_models[model.public_id] = model
            self.public_model_order.append(model.public_id)

        if not self.public_models:
            legacy_only = self._build_public_model(self._default_catalog_document()["models"][0])
            if legacy_only is not None:
                self.public_models[legacy_only.public_id] = legacy_only
                self.public_model_order = [legacy_only.public_id]

        requested_default_text = str(document.get("default_text_model") or settings.fixed_model or "").strip()
        self.default_text_model_id = self._pick_default_model(requested_default_text, TEXT_ENDPOINTS)

        requested_default_embedding = str(
            document.get("default_embedding_model") or getattr(settings, "embedding_model", "") or ""
        ).strip()
        self.default_embedding_model_id = self._pick_default_model(requested_default_embedding, EMBEDDING_ENDPOINTS)

        requested_default_image = str(document.get("default_image_model") or "").strip()
        self.default_image_model_id = self._pick_default_model(requested_default_image, IMAGE_ENDPOINTS)

        requested_default_video = str(document.get("default_video_model") or "").strip()
        self.default_video_model_id = self._pick_default_model(requested_default_video, VIDEO_ENDPOINTS)

    def _build_explicit_legacy_backend(self, public_model: PublicModelConfig) -> Optional[ModelConfig]:
        target_model = str(public_model.upstream_model or public_model.provider_model or "").strip()
        if not target_model:
            return None

        primary = self.get(PREMIUM)
        return ModelConfig(
            model_id=target_model,
            upstream_url=primary.upstream_url,
            api_key=primary.api_key,
            price_input_per_million=primary.price_input_per_million,
            price_output_per_million=primary.price_output_per_million,
            strip_unsupported=primary.strip_unsupported or public_model.strip_unsupported or _is_codex_like(target_model),
            auth_style=primary.auth_style,
        )

    def _apply_channel_route(
        self,
        public_model: PublicModelConfig,
        backend: ModelConfig,
        endpoint: str,
        *,
        exclude_channel_ids: Tuple[str, ...] = (),
        fallback_from_channel_id: str = "",
        route_attempt: int = 0,
    ) -> Optional[ModelConfig]:
        choice = channel_router.select_for_model(
            public_model,
            backend,
            endpoint,
            exclude_channel_ids=exclude_channel_ids,
        )
        if choice is None:
            return None
        return ModelConfig(
            model_id=choice.provider_model,
            upstream_url=choice.upstream_url,
            api_key=choice.api_key,
            price_input_per_million=backend.price_input_per_million,
            price_output_per_million=backend.price_output_per_million,
            strip_unsupported=backend.strip_unsupported or public_model.strip_unsupported,
            auth_style=choice.auth_style or backend.auth_style,
            channel_id=choice.channel_id,
            route_id=choice.route_id,
            channel_type=choice.channel_type,
            provider_platform=choice.provider_platform,
            provider_account_fingerprint=choice.provider_account_fingerprint,
            transform_profile=choice.transform_profile,
            cost_tier=choice.cost_tier,
            fallback_from_channel_id=fallback_from_channel_id,
            route_attempt=route_attempt if route_attempt > 0 else choice.route_attempt,
            channel_priority=choice.priority,
            channel_weight=choice.weight,
            allowed_fails=choice.allowed_fails,
            cooldown_seconds=choice.cooldown_seconds,
        )

    def resolve_channel_fallback(
        self,
        public_model: PublicModelConfig,
        previous_backend: ModelConfig,
        endpoint: str,
        *,
        exclude_channel_ids: Tuple[str, ...] = (),
    ) -> Optional[ModelConfig]:
        previous_channel_id = (previous_backend.channel_id or "").strip()
        if not previous_channel_id:
            return None
        excluded = tuple(dict.fromkeys(
            item for item in (*exclude_channel_ids, previous_channel_id) if item
        ))
        fallback_from_channel_id = ",".join(excluded)
        return self._apply_channel_route(
            public_model,
            previous_backend,
            endpoint,
            exclude_channel_ids=excluded,
            fallback_from_channel_id=fallback_from_channel_id,
            route_attempt=int(previous_backend.route_attempt or 0) + 1,
        )

    def resolve_system_fallback(
        self,
        public_model: PublicModelConfig,
        previous_backend: ModelConfig,
        endpoint: str,
        messages: Optional[List[dict]] = None,
        tools: Optional[list] = None,
        *,
        lock_model_selection: bool = False,
    ) -> Optional[ResolvedModel]:
        previous_channel_id = (previous_backend.channel_id or "").strip()
        if not previous_channel_id or previous_channel_id.startswith("system:"):
            return None
        attempted = []
        for raw in ((previous_backend.fallback_from_channel_id or ""), previous_channel_id):
            attempted.extend(item.strip() for item in str(raw or "").split(",") if item.strip())
        fallback_from_channel_id = ",".join(dict.fromkeys(attempted))

        execution_profile = self._resolve_execution_profile(public_model, endpoint)
        if endpoint in EMBEDDING_ENDPOINTS:
            backend = self.get(EMBEDDING)
            route_reason = f"catalog:{public_model.public_id}:embedding:system_fallback"
        elif public_model.routing_mode == "legacy_auto":
            explicit_backend = self._build_explicit_legacy_backend(public_model) if lock_model_selection else None
            if explicit_backend is not None:
                backend = explicit_backend
                route_reason = f"catalog:{public_model.public_id}:legacy_explicit:system_fallback"
            else:
                backend, inner_reason = resolve(messages or [], tools, execution_profile=execution_profile)
                route_reason = f"catalog:{public_model.public_id}:{inner_reason}:system_fallback"
        else:
            backend = ModelConfig(
                model_id=public_model.upstream_model,
                upstream_url=public_model.upstream_url,
                api_key=public_model.api_key,
                price_input_per_million=public_model.price_input_per_million,
                price_output_per_million=public_model.price_output_per_million,
                strip_unsupported=public_model.strip_unsupported,
                auth_style=public_model.auth_style,
            )
            route_reason = f"catalog:{public_model.public_id}:{public_model.delivery_lane}:system_fallback"

        if not (backend.upstream_url and backend.api_key and backend.model_id):
            return None

        if public_model.routing_mode == "legacy_auto":
            system_channel_id = "system:legacy_cpa"
            system_channel_type = "account_pool"
            system_platform = "legacy_cpa"
        elif public_model.delivery_lane == "cpa_gemini":
            system_channel_id = ""
            system_channel_type = "account_pool"
            system_platform = "cpa_gemini"
        else:
            system_channel_id = f"system:{public_model.delivery_lane or 'catalog'}"[:32]
            system_channel_type = "openai_compatible"
            system_platform = public_model.delivery_lane or "catalog"

        backend = ModelConfig(
            model_id=backend.model_id,
            upstream_url=backend.upstream_url,
            api_key=backend.api_key,
            price_input_per_million=backend.price_input_per_million,
            price_output_per_million=backend.price_output_per_million,
            strip_unsupported=backend.strip_unsupported or public_model.strip_unsupported,
            auth_style=backend.auth_style,
            channel_id=system_channel_id,
            channel_type=system_channel_type,
            provider_platform=system_platform,
            provider_account_fingerprint=backend.provider_account_fingerprint,
            transform_profile=backend.transform_profile,
            cost_tier=backend.cost_tier,
            fallback_from_channel_id=fallback_from_channel_id,
            route_attempt=int(previous_backend.route_attempt or 0) + 1,
        )
        return ResolvedModel(
            public_model=public_model,
            backend=backend,
            execution_profile=execution_profile.profile_id,
            execution_pool=execution_profile.pool_id,
            route_reason=route_reason,
            lock_model_selection=lock_model_selection,
        )

    def _resolved_with_channel_route(
        self,
        *,
        public_model: PublicModelConfig,
        backend: ModelConfig,
        endpoint: str,
        execution_profile: ExecutionProfile,
        route_reason: str,
        lock_model_selection: bool = False,
    ) -> ResolvedModel:
        routed_backend = self._apply_channel_route(public_model, backend, endpoint)
        if routed_backend is not None:
            backend = routed_backend
            route_reason = f"{route_reason}:channel:{backend.channel_id}"
        return ResolvedModel(
            public_model=public_model,
            backend=backend,
            execution_profile=execution_profile.profile_id,
            execution_pool=execution_profile.pool_id,
            route_reason=route_reason,
            lock_model_selection=lock_model_selection,
        )

    def _pick_default_model(self, requested_id: str, allowed_caps: frozenset[str]) -> str:
        if requested_id and requested_id in self.public_models:
            model = self.public_models[requested_id]
            if allowed_caps.intersection(model.capabilities):
                return requested_id

        for model_id in self.public_model_order:
            model = self.public_models[model_id]
            if allowed_caps.intersection(model.capabilities):
                return model_id
        return ""

    def ensure_initialized(self) -> None:
        if (
            not self._initialized
            or self._alias_override_state != self._current_alias_override_state()
            or self._pricing_override_state != self._current_pricing_override_state()
            or self._system_settings_state_snapshot != self._current_system_settings_state()
        ):
            self.init_from_settings()

    def _current_alias_override_state(self) -> Tuple[str, int]:
        if self._runtime_alias_overrides is not None:
            return ("runtime", self._runtime_alias_override_version)
        raw_path = (getattr(settings, "model_alias_overrides_path", "") or "").strip()
        if not raw_path:
            return ("", -1)
        path = _catalog_path(raw_path)
        try:
            return (str(path), path.stat().st_mtime_ns)
        except OSError:
            return (str(path), -1)

    def _current_pricing_override_state(self) -> Tuple[str, int]:
        if self._runtime_pricing_overrides is not None:
            return ("runtime", self._runtime_pricing_override_version)
        return ("", -1)

    def _system_settings_state(self) -> Tuple[str, int]:
        if self._runtime_system_settings is not None:
            return ("runtime", self._runtime_system_settings_version)
        return ("env", 0)

    def _current_system_settings_state(self) -> Tuple[str, int]:
        return self._system_settings_state()

    def set_runtime_alias_overrides(self, overrides: Dict[str, Dict[str, Any]], version: int = 0) -> None:
        self._runtime_alias_overrides = _safe_alias_overrides({"aliases": overrides})
        self._runtime_alias_override_version = int(version or 0)
        self._initialized = False

    def clear_runtime_alias_overrides(self) -> None:
        self._runtime_alias_overrides = None
        self._runtime_alias_override_version = 0
        self._initialized = False

    def set_runtime_pricing_overrides(self, overrides: Dict[str, Dict[str, Any]], version: int = 0) -> None:
        self._runtime_pricing_overrides = {
            str(key or "").strip(): dict(value or {})
            for key, value in (overrides or {}).items()
            if str(key or "").strip()
        }
        self._runtime_pricing_override_version = int(version or 0)
        self._initialized = False

    def clear_runtime_pricing_overrides(self) -> None:
        self._runtime_pricing_overrides = None
        self._runtime_pricing_override_version = 0
        self._initialized = False

    def set_runtime_system_settings(self, runtime_settings: Dict[str, Any], version: int = 0) -> None:
        self._runtime_system_settings = dict(runtime_settings or {})
        self._runtime_system_settings_version = int(version or 0)
        self._initialized = False

    def clear_runtime_system_settings(self) -> None:
        self._runtime_system_settings = None
        self._runtime_system_settings_version = 0
        self._initialized = False

    def current_system_settings(self) -> Dict[str, Any]:
        if self._runtime_system_settings is not None:
            return dict(self._runtime_system_settings)
        return {}

    def current_claude_compat_provider(self) -> str:
        runtime_value = self.current_system_settings().get("claude_compat_provider")
        if runtime_value not in (None, ""):
            return _normalized_claude_compat_provider(runtime_value)
        return _normalized_claude_compat_provider(getattr(settings, "claude_compat_provider", ""))

    def get(self, slot: str) -> ModelConfig:
        self.ensure_initialized()
        return self.models.get(slot, self.models[PREMIUM])

    def list_model_ids(self) -> List[str]:
        self.ensure_initialized()
        return list(self.public_model_order)

    def list_public_models(self, capability: Optional[str] = None) -> List[PublicModelConfig]:
        self.ensure_initialized()
        result: List[PublicModelConfig] = []
        for model_id in self.public_model_order:
            model = self.public_models[model_id]
            if capability and capability not in model.capabilities:
                continue
            result.append(model)
        return result

    def get_public_model(self, model_id: str) -> Optional[PublicModelConfig]:
        self.ensure_initialized()
        return self.public_models.get(model_id)

    def list_admin_aliases(self) -> List[Dict[str, Any]]:
        self.ensure_initialized()
        aliases: List[Dict[str, Any]] = []
        seen = set()
        ordered_ids = [*self.public_model_order, *self._raw_public_models.keys()]
        for model_id in ordered_ids:
            if model_id in seen:
                continue
            seen.add(model_id)
            raw = self._raw_public_models.get(model_id) or {}
            model = self.public_models.get(model_id)
            capabilities = tuple(model.capabilities if model else raw.get("capabilities") or ())
            enabled = raw.get("enabled")
            alias = {
                "id": model_id,
                "enabled": _as_bool(enabled, True) if enabled is not None else model is not None,
                "routable": model is not None,
                "override_active": model_id in self.alias_overrides,
                "override": self.alias_overrides.get(model_id) or {},
                "owned_by": (model.owned_by if model else str(raw.get("owned_by") or "coincoin")),
                "provider_name": (model.provider_name if model else str(raw.get("provider_name") or "")),
                "provider_model": (model.provider_model if model else str(raw.get("provider_model") or "")),
                "routing_mode": (model.routing_mode if model else str(raw.get("routing_mode") or "direct")),
                "delivery_lane": (model.delivery_lane if model else str(raw.get("delivery_lane") or "")),
                "upstream_model": (model.upstream_model if model else str(raw.get("upstream_model") or "")),
                "capabilities": list(capabilities),
                "billable_sku": (model.billable_sku if model else str(raw.get("billable_sku") or model_id)),
                "base_price_input_per_million": (model.base_price_input_per_million if model else _as_int(raw.get("price_input_per_million"), 0)),
                "base_price_output_per_million": (model.base_price_output_per_million if model else _as_int(raw.get("price_output_per_million"), 0)),
                "base_price_per_image_cents": (model.base_price_per_image_cents if model else _as_float(raw.get("price_per_image_cents"), 0.0)),
                "base_price_per_video_cents": (model.base_price_per_video_cents if model else _as_float(raw.get("price_per_video_cents"), 0.0)),
                "price_input_per_million": (model.price_input_per_million if model else _as_int(raw.get("price_input_per_million"), 0)),
                "price_output_per_million": (model.price_output_per_million if model else _as_int(raw.get("price_output_per_million"), 0)),
                "price_per_image_cents": (model.price_per_image_cents if model else _as_float(raw.get("price_per_image_cents"), 0.0)),
                "price_per_video_cents": (model.price_per_video_cents if model else _as_float(raw.get("price_per_video_cents"), 0.0)),
                "effective_cached_input_per_million": (model.effective_cached_input_per_million if model else 0.0),
                "pricing_mode": (model.pricing_mode if model else "explicit_price"),
                "model_multiplier": (model.model_multiplier if model else 1.0),
                "output_multiplier": (model.output_multiplier if model else 1.0),
                "cache_read_multiplier": (model.cache_read_multiplier if model else _as_float(getattr(settings, "cache_discount_rate", 0.0), 0.0)),
                "image_multiplier": (model.image_multiplier if model else 1.0),
                "video_multiplier": (model.video_multiplier if model else 1.0),
                "price_version": (model.price_version if model else 0),
            }
            aliases.append(alias)
        return aliases

    def candidate_alias_targets(self, alias_id: str) -> List[Dict[str, Any]]:
        self.ensure_initialized()
        source = self.public_models.get(alias_id) or self._raw_public_models.get(alias_id)
        if not source:
            return []

        if isinstance(source, PublicModelConfig):
            source_caps = set(source.capabilities)
            source_lane = source.delivery_lane
            source_routing_mode = source.routing_mode
        else:
            source_caps = set(source.get("capabilities") or [])
            source_lane = str(source.get("delivery_lane") or "").strip().lower()
            source_routing_mode = str(source.get("routing_mode") or "direct").strip().lower()

        candidates = []
        for item in self.list_admin_aliases():
            if item["id"] == alias_id:
                continue
            if set(item.get("capabilities") or []) != source_caps:
                continue
            if item.get("delivery_lane") != source_lane:
                continue
            if item.get("routing_mode") != source_routing_mode:
                continue
            if not item.get("provider_model") and not item.get("upstream_model"):
                continue
            candidates.append(item)
        return candidates

    def get_admin_alias(self, alias_id: str) -> Optional[Dict[str, Any]]:
        for item in self.list_admin_aliases():
            if item["id"] == alias_id:
                return item
        return None

    def has_routable_models(self) -> bool:
        self.ensure_initialized()
        return bool(self.public_models or self.models)

    def _select_public_model(self, requested_model: Optional[str], endpoint: str) -> PublicModelConfig:
        self.ensure_initialized()

        model_id = (requested_model or "").strip()
        if not model_id:
            if endpoint in IMAGE_ENDPOINTS:
                model_id = self.default_image_model_id
            elif endpoint in VIDEO_ENDPOINTS:
                model_id = self.default_video_model_id
            elif endpoint in EMBEDDING_ENDPOINTS:
                model_id = self.default_embedding_model_id
            else:
                model_id = self.default_text_model_id

        if not model_id:
            raise ModelCapabilityError(f"No default model configured for endpoint '{endpoint}'")

        model = self.public_models.get(model_id)
        if model is None:
            raise UnknownModelError(f"Model '{model_id}' is not available")
        if endpoint not in model.capabilities:
            raise ModelCapabilityError(f"Model '{model_id}' does not support endpoint '{endpoint}'")
        return model

    def resolve_public_model(
        self,
        requested_model: Optional[str],
        endpoint: str,
        messages: Optional[List[dict]] = None,
        tools: Optional[list] = None,
    ) -> ResolvedModel:
        public_model = self._select_public_model(requested_model, endpoint)
        explicit_requested = bool((requested_model or "").strip())
        execution_profile = self._resolve_execution_profile(public_model, endpoint)
        if endpoint in EMBEDDING_ENDPOINTS:
            return self._resolved_with_channel_route(
                public_model=public_model,
                backend=self.get(EMBEDDING),
                endpoint=endpoint,
                execution_profile=execution_profile,
                route_reason=f"catalog:{public_model.public_id}:{public_model.delivery_lane or 'upstream_direct'}",
            )
        if public_model.routing_mode == "legacy_auto":
            if explicit_requested:
                explicit_backend = self._build_explicit_legacy_backend(public_model)
                if explicit_backend is not None:
                    return self._resolved_with_channel_route(
                        public_model=public_model,
                        backend=explicit_backend,
                        endpoint=endpoint,
                        execution_profile=execution_profile,
                        route_reason=f"catalog:{public_model.public_id}:legacy_explicit",
                        lock_model_selection=True,
                    )
            backend, route_reason = resolve(messages or [], tools, execution_profile=execution_profile)
            return self._resolved_with_channel_route(
                public_model=public_model,
                backend=backend,
                endpoint=endpoint,
                execution_profile=execution_profile,
                route_reason=f"catalog:{public_model.public_id}:{route_reason}",
            )

        if public_model.routing_mode == "route_only" or public_model.delivery_lane == "route_only":
            backend = ModelConfig(
                model_id=public_model.upstream_model or public_model.provider_model or public_model.public_id,
                upstream_url="",
                api_key="",
                price_input_per_million=public_model.price_input_per_million,
                price_output_per_million=public_model.price_output_per_million,
                strip_unsupported=public_model.strip_unsupported,
                auth_style=public_model.auth_style,
            )
            routed_backend = self._apply_channel_route(public_model, backend, endpoint)
            if routed_backend is None:
                raise ModelCapabilityError(
                    f"Model '{public_model.public_id}' requires an active provider channel route for endpoint '{endpoint}'"
                )
            return ResolvedModel(
                public_model=public_model,
                backend=routed_backend,
                execution_profile=execution_profile.profile_id,
                execution_pool=execution_profile.pool_id,
                route_reason=f"catalog:{public_model.public_id}:route_only:channel:{routed_backend.channel_id}",
            )

        backend = ModelConfig(
            model_id=public_model.upstream_model,
            upstream_url=public_model.upstream_url,
            api_key=public_model.api_key,
            price_input_per_million=public_model.price_input_per_million,
            price_output_per_million=public_model.price_output_per_million,
            strip_unsupported=public_model.strip_unsupported,
            auth_style=public_model.auth_style,
        )
        return self._resolved_with_channel_route(
            public_model=public_model,
            backend=backend,
            endpoint=endpoint,
            execution_profile=execution_profile,
            route_reason=f"catalog:{public_model.public_id}:{public_model.delivery_lane}",
        )

    def _resolve_execution_profile(self, public_model: PublicModelConfig, endpoint: str) -> ExecutionProfile:
        if endpoint in EMBEDDING_ENDPOINTS:
            return ExecutionProfile(
                profile_id="embedding_direct",
                pool_id="upstream_embedding_pool",
                legacy_default_slot=EMBEDDING,
                honor_tool_routing=False,
            )

        if public_model.routing_mode != "legacy_auto":
            return ExecutionProfile(
                profile_id=f"{public_model.delivery_lane}_direct",
                pool_id=f"{public_model.delivery_lane}_direct_pool",
                honor_tool_routing=False,
            )

        metadata = public_model.metadata or {}
        legacy_default_slot = str(metadata.get("legacy_default_slot") or CHEAP).strip().lower() or CHEAP
        if legacy_default_slot not in LEGACY_ROUTE_SLOTS:
            logger.warning(
                "public model %s has unsupported legacy_default_slot=%r; falling back to %s",
                public_model.public_id,
                legacy_default_slot,
                CHEAP,
            )
            legacy_default_slot = CHEAP

        return ExecutionProfile(
            profile_id=str(metadata.get("execution_profile") or "legacy_general").strip() or "legacy_general",
            pool_id=str(metadata.get("execution_pool") or "cpa_general_pool").strip() or "cpa_general_pool",
            legacy_default_slot=legacy_default_slot,
            honor_tool_routing=_as_bool(metadata.get("honor_tool_routing"), True),
        )


def _alias_overrides_path() -> Path:
    raw_path = (getattr(settings, "model_alias_overrides_path", "") or "").strip()
    if not raw_path:
        raise ModelResolutionError("model alias overrides path is not configured")
    return _catalog_path(raw_path)


def load_alias_override_document() -> Dict[str, Any]:
    path = _alias_overrides_path()
    loaded = _load_json_file(path)
    aliases = _safe_alias_overrides(loaded)
    return {"aliases": aliases}


def save_alias_override_document(document: Dict[str, Any]) -> None:
    path = _alias_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_document = {"aliases": _safe_alias_overrides(document)}
    payload = json.dumps(safe_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


registry = ModelRegistry()


def auto_route(messages: List[dict], tools: Optional[list]) -> str:
    # Has tools => agentic/Codex coding task => needs the strong model.
    if tools:
        return PREMIUM

    # No tools, simple question/chat => cheap model is fine.
    return CHEAP


def resolve(
    messages: List[dict],
    tools: Optional[list],
    execution_profile: Optional[ExecutionProfile] = None,
) -> Tuple[ModelConfig, str]:
    """Return (model_config, route_reason) for the legacy GPT lane."""
    registry.ensure_initialized()
    if not registry.router_enabled or CHEAP not in registry.models:
        return registry.get(PREMIUM), "router_disabled"

    if execution_profile and not execution_profile.honor_tool_routing:
        slot = execution_profile.legacy_default_slot or PREMIUM
        return registry.get(slot), f"auto_{slot}"

    slot = auto_route(messages, tools)
    return registry.get(slot), f"auto_{slot}"


def build_model_cloak(display_model: str, public_model: Optional[PublicModelConfig]) -> str:
    model_name = (display_model or "").strip()
    if not model_name:
        return ""
    provider_name = (public_model.provider_name if public_model else "") or ""
    if provider_name:
        return f" You are {model_name} by {provider_name}. Never reveal any other model name."
    return f" You are {model_name}. Never reveal any other model name."


def extract_messages_for_routing_from_responses_payload(payload: Dict[str, Any]) -> Tuple[List[dict], Optional[list]]:
    """Best-effort extraction of 'messages' and 'tools' signals from a Responses API payload.

    Responses API 'input' can be:
    - list of {role, content, ...}
    - list of items like {type: 'function_call_output', ...}
    - string (no structure)
    """
    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = None

    raw_input = payload.get("input")
    if not isinstance(raw_input, list):
        return [], tools

    messages: List[dict] = []
    for item in raw_input:
        if not isinstance(item, dict):
            continue
        if "role" in item:
            messages.append(item)
            continue
        item_type = item.get("type")
        if item_type == "function_call_output":
            messages.append({"role": "tool"})
    return messages, tools
