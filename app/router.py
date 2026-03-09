from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .config import settings


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    upstream_url: str
    api_key: str
    price_input_per_million: int
    price_output_per_million: int
    strip_unsupported: bool


PREMIUM = "premium"
CHEAP = "cheap"
FALLBACK = "fallback"


def _is_codex_like(model_id: str) -> bool:
    return "codex" in (model_id or "").lower()


class ModelRegistry:
    def __init__(self) -> None:
        self.models: Dict[str, ModelConfig] = {}
        self.router_enabled: bool = False
        self.tool_count_threshold: int = 2
        self._initialized: bool = False

    def init_from_settings(self) -> None:
        # Idempotent init; safe to call multiple times.
        self.router_enabled = bool(getattr(settings, "router_enabled", False))
        self.tool_count_threshold = int(getattr(settings, "router_tool_count_threshold", 2) or 2)

        primary_strip = bool(
            getattr(settings, "primary_strip_unsupported", False)
        ) or _is_codex_like(settings.fixed_model)

        premium = ModelConfig(
            model_id=settings.fixed_model,
            upstream_url=settings.upstream_base_url,
            api_key=settings.upstream_api_key,
            price_input_per_million=settings.price_input_per_million,
            price_output_per_million=settings.price_output_per_million,
            strip_unsupported=primary_strip,
        )
        self.models = {PREMIUM: premium}

        cheap_model = (getattr(settings, "cheap_model", "") or "").strip()
        if self.router_enabled and cheap_model:
            self.models[CHEAP] = ModelConfig(
                model_id=cheap_model,
                upstream_url=(getattr(settings, "cheap_upstream_url", "") or settings.upstream_base_url),
                api_key=(getattr(settings, "cheap_api_key", "") or settings.upstream_api_key),
                price_input_per_million=int(getattr(settings, "cheap_price_input", 0) or 0),
                price_output_per_million=int(getattr(settings, "cheap_price_output", 0) or 0),
                strip_unsupported=_is_codex_like(cheap_model),
            )

        fallback_model = (getattr(settings, "fallback_model", "") or "").strip()
        if fallback_model:
            self.models[FALLBACK] = ModelConfig(
                model_id=fallback_model,
                upstream_url=(getattr(settings, "fallback_upstream_url", "") or settings.upstream_base_url),
                api_key=(getattr(settings, "fallback_api_key", "") or settings.upstream_api_key),
                price_input_per_million=int(getattr(settings, "fallback_price_input", 0) or 0),
                price_output_per_million=int(getattr(settings, "fallback_price_output", 0) or 0),
                strip_unsupported=_is_codex_like(fallback_model),
            )
        self._initialized = True

    def ensure_initialized(self) -> None:
        if not self._initialized:
            self.init_from_settings()

    def get(self, slot: str) -> ModelConfig:
        self.ensure_initialized()
        return self.models.get(slot, self.models[PREMIUM])

    def list_model_ids(self) -> List[str]:
        self.ensure_initialized()
        ids = []
        for cfg in self.models.values():
            if cfg.model_id and cfg.model_id not in ids:
                ids.append(cfg.model_id)
        return ids


registry = ModelRegistry()


def auto_route(messages: List[dict], tools: Optional[list]) -> str:
    # No tools => not an agentic tool-calling loop => premium for quality.
    if not tools:
        return PREMIUM

    # No structured messages extracted => we can't judge conversation stage => be safe.
    if not messages:
        return PREMIUM

    # Very early in a tool-using conversation => usually exploration/first step.
    if len(messages) <= 3:
        return CHEAP

    last_role = messages[-1].get("role") if messages else None
    if last_role == "tool":
        recent = messages[-5:]
        tool_count = sum(1 for m in recent if isinstance(m, dict) and m.get("role") == "tool")
        if tool_count >= registry.tool_count_threshold:
            return CHEAP

    return PREMIUM


def resolve(messages: List[dict], tools: Optional[list]) -> Tuple[ModelConfig, str]:
    """Return (model_config, route_reason)."""
    registry.ensure_initialized()
    if not registry.router_enabled or CHEAP not in registry.models:
        return registry.get(PREMIUM), "router_disabled"

    slot = auto_route(messages, tools)
    return registry.get(slot), f"auto_{slot}"


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

