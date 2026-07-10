from __future__ import annotations

import hashlib
import re
from typing import Any


_OPENAI_PROMPT_CACHE_MODELS = {
    "gpt-5-5",
    "gpt-5-6",
    "gpt-5-6-sol",
    "gpt-5-6-terra",
    "gpt-5-6-luna",
}

_OPENAI_PROMPT_CACHE_24H_MODELS = {
    "gpt-5-5",
    "gpt-5-6",
    "gpt-5-6-sol",
    "gpt-5-6-terra",
    "gpt-5-6-luna",
}


def _metadata(public_model: Any) -> dict[str, Any]:
    metadata = getattr(public_model, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _canonical_model_id(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if not text:
        return ""
    text = re.sub(r"(?<=\d)\.(?=\d)", "-", text)
    text = re.sub(r"-+", "-", text)
    return text


def _candidate_model_ids(display_model: str, public_model: Any, effective_backend_model: str) -> tuple[str, ...]:
    candidates = (
        display_model,
        effective_backend_model,
        getattr(public_model, "public_id", ""),
        getattr(public_model, "provider_model", ""),
        getattr(public_model, "upstream_model", ""),
        getattr(public_model, "billable_sku", ""),
    )
    result: list[str] = []
    for item in candidates:
        normalized = _canonical_model_id(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _uses_prompt_cache_key(
    metadata: dict[str, Any],
    model_ids: tuple[str, ...],
    *,
    include_openai_models: bool = False,
) -> bool:
    if str(metadata.get("compat_family") or "").strip().lower() == "claude-code":
        return True
    if metadata.get("prompt_cache_key") is True or metadata.get("prompt_cache_key_enabled") is True:
        return True
    if not include_openai_models:
        return False
    execution_profile = str(metadata.get("execution_profile") or "").strip().lower()
    if execution_profile in {"legacy-coding", "legacy_coding"}:
        return True
    return any("codex" in item or item in _OPENAI_PROMPT_CACHE_MODELS for item in model_ids)


def build_openai_prompt_cache_retention(
    display_model: str,
    public_model: Any,
    *,
    effective_backend_model: str = "",
) -> str:
    backend_model = _canonical_model_id(effective_backend_model)
    if backend_model:
        return "24h" if backend_model in _OPENAI_PROMPT_CACHE_24H_MODELS else ""
    model_ids = _candidate_model_ids(display_model, public_model, effective_backend_model)
    if any(item in _OPENAI_PROMPT_CACHE_24H_MODELS for item in model_ids):
        return "24h"
    return ""


def apply_default_openai_prompt_cache_retention(
    payload: dict[str, Any],
    display_model: str,
    public_model: Any,
    *,
    effective_backend_model: str = "",
) -> None:
    retention = build_openai_prompt_cache_retention(
        display_model,
        public_model,
        effective_backend_model=effective_backend_model,
    )
    if retention:
        payload["prompt_cache_retention"] = retention
    else:
        payload.pop("prompt_cache_retention", None)


def build_channel_affinity_key(
    user: Any,
    api_key_id: str,
    endpoint: str,
    requested_model: str = "",
    prompt_cache_key: Any = "",
) -> str:
    seed = (
        f"{getattr(user, 'id', '')}:"
        f"{api_key_id or ''}:"
        f"{str(endpoint or '').strip().lower()}:"
        f"{_canonical_model_id(requested_model)}:"
        f"{str(prompt_cache_key or '').strip()}"
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"aff-{digest}"


def build_claude_code_prompt_cache_key(
    user: Any,
    api_key_id: str,
    display_model: str,
    public_model: Any,
    *,
    effective_backend_model: str = "",
    include_openai_models: bool = False,
) -> str:
    metadata = _metadata(public_model)
    model_ids = _candidate_model_ids(display_model, public_model, effective_backend_model)
    if not _uses_prompt_cache_key(metadata, model_ids, include_openai_models=include_openai_models):
        return ""
    cache_family = "claude-code" if str(metadata.get("compat_family") or "").strip().lower() == "claude-code" else "openai-responses"
    canonical_model = model_ids[0] if model_ids else _canonical_model_id(display_model)
    canonical_backend = _canonical_model_id(effective_backend_model)
    seed = (
        f"{getattr(user, 'id', '')}:"
        f"{api_key_id or ''}:"
        f"{cache_family}:"
        f"{canonical_model}:"
        f"{canonical_backend}"
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"cc-{digest}"
