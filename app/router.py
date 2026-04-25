from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


@dataclass(frozen=True)
class PublicModelConfig:
    public_id: str
    owned_by: str = "coincoin"
    provider_name: str = ""
    capabilities: Tuple[str, ...] = ()
    routing_mode: str = "direct"  # direct | legacy_auto
    delivery_lane: str = "upstream_direct"  # legacy | gateway | vertex_direct | upstream_direct
    upstream_model: str = ""
    provider_model: str = ""
    upstream_url: str = ""
    api_key: str = ""
    auth_style: str = "bearer"
    price_input_per_million: int = 0
    price_output_per_million: int = 0
    price_per_image_cents: int = 0
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
DELIVERY_LANES = frozenset({"legacy", "gateway", "vertex_direct", "upstream_direct"})
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(:-([^}]*))?\}")
_ROOT_DIR = Path(__file__).resolve().parent.parent


def _is_codex_like(model_id: str) -> bool:
    return "codex" in (model_id or "").lower()


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


def _resolve_placeholders(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda match: _lookup_placeholder(match.group(1), match.group(3) or ""),
            value,
        )
    if isinstance(value, list):
        return [_resolve_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_placeholders(item) for key, item in value.items()}
    return value


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
        self._initialized: bool = False

    def init_from_settings(self) -> None:
        # Idempotent init; safe to call multiple times.
        self.router_enabled = bool(getattr(settings, "router_enabled", False))
        self.tool_count_threshold = int(getattr(settings, "router_tool_count_threshold", 2) or 2)
        self._init_legacy_backends()
        self._init_public_catalog()
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
            model_id=settings.fixed_model,
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
                model_id=cheap_model,
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
                model_id=fallback_model,
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
        catalog_path = Path(raw_path)
        if not catalog_path.is_absolute():
            catalog_path = _ROOT_DIR / catalog_path
        if not catalog_path.is_file():
            logger.info("model catalog not found at %s; using default legacy-only catalog", catalog_path)
            return self._default_catalog_document()

        try:
            loaded = json.loads(catalog_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to load model catalog %s: %s", catalog_path, exc)
            return self._default_catalog_document()
        return _resolve_placeholders(loaded)

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
        default_delivery_lane = "legacy" if routing_mode == "legacy_auto" else "upstream_direct"
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
        upstream_url = str(raw.get("upstream_url") or "").strip()
        api_key = str(raw.get("api_key") or "").strip()
        auth_style = str(raw.get("auth_style") or settings.gateway_auth_style or "bearer").strip() or "bearer"

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
            price_input_per_million=_as_int(raw.get("price_input_per_million"), 0),
            price_output_per_million=_as_int(raw.get("price_output_per_million"), 0),
            price_per_image_cents=_as_int(raw.get("price_per_image_cents"), 0),
            billable_sku=str(raw.get("billable_sku") or public_id).strip() or public_id,
            created=_as_int(raw.get("created"), 1700000000),
            strip_unsupported=_as_bool(raw.get("strip_unsupported"), False),
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        )

    def _init_public_catalog(self) -> None:
        self.public_models = {}
        self.public_model_order = []

        document = self._load_catalog_document()
        raw_models = document.get("models")
        if not isinstance(raw_models, list):
            raw_models = []

        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            enabled = raw.get("enabled")
            if enabled is not None and not _as_bool(enabled, True):
                continue
            model = self._build_public_model(raw)
            if model is None:
                continue
            if model.routing_mode != "legacy_auto":
                if not (model.upstream_model and model.upstream_url and model.api_key):
                    logger.warning(
                        "skipping public model %s because upstream gateway config is incomplete",
                        model.public_id,
                    )
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
        if not self._initialized:
            self.init_from_settings()

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

    def has_routable_models(self) -> bool:
        self.ensure_initialized()
        return bool(self.public_models or self.models)

    def _select_public_model(self, requested_model: Optional[str], endpoint: str) -> PublicModelConfig:
        self.ensure_initialized()

        model_id = (requested_model or "").strip()
        if not model_id:
            if endpoint in IMAGE_ENDPOINTS:
                model_id = self.default_image_model_id
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
        execution_profile = self._resolve_execution_profile(public_model, endpoint)
        if endpoint in EMBEDDING_ENDPOINTS:
            return ResolvedModel(
                public_model=public_model,
                backend=self.get(EMBEDDING),
                execution_profile=execution_profile.profile_id,
                execution_pool=execution_profile.pool_id,
                route_reason=f"catalog:{public_model.public_id}:{public_model.delivery_lane or 'upstream_direct'}",
            )
        if public_model.routing_mode == "legacy_auto":
            backend, route_reason = resolve(messages or [], tools, execution_profile=execution_profile)
            return ResolvedModel(
                public_model=public_model,
                backend=backend,
                execution_profile=execution_profile.profile_id,
                execution_pool=execution_profile.pool_id,
                route_reason=f"catalog:{public_model.public_id}:{route_reason}",
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
        return ResolvedModel(
            public_model=public_model,
            backend=backend,
            execution_profile=execution_profile.profile_id,
            execution_pool=execution_profile.pool_id,
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
