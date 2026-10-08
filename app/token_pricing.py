"""Whole-request context tiers, shared by billing and the public price catalog."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ContextPricingTier:
    above_input_tokens: int
    input_multiplier: float
    output_multiplier: float


# Exact public billing IDs only: provider routing must not change a user's price.
# Source: https://developers.openai.com/api/docs/pricing (2026-10-08).
GPT_LONG_CONTEXT_MODELS = frozenset({
    "gpt-5.5", "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-6", "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna",
})


def default_gpt_pricing(model_id: str) -> dict[str, Any]:
    if model_id not in GPT_LONG_CONTEXT_MODELS:
        return {}
    pricing: dict[str, Any] = {
        "context_pricing_tiers": [asdict(ContextPricingTier(272_000, 2.0, 1.5))],
    }
    if model_id != "gpt-5.5":
        pricing["cache_creation_multiplier"] = 1.25
    if model_id == "gpt-6.1-sol":
        pricing["cache_read_multiplier"] = 0.05
    return pricing


def parse_context_pricing_tiers(raw: Any) -> tuple[ContextPricingTier, ...]:
    """Validate catalog rules once; [] explicitly opts a model out of tiers."""
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError("context_pricing_tiers must be a list")
    tiers = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("context_pricing_tiers entries must be objects")
        threshold = item.get("above_input_tokens")
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
            raise ValueError("above_input_tokens must be a positive integer")
        multipliers = []
        for key in ("input_multiplier", "output_multiplier"):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"context tier {key} must be finite and positive")
            multipliers.append(float(value))
        tiers.append(ContextPricingTier(threshold, *multipliers))
    tiers.sort(key=lambda tier: tier.above_input_tokens)
    if len({tier.above_input_tokens for tier in tiers}) != len(tiers):
        raise ValueError("context_pricing_tiers thresholds must be unique")
    return tuple(tiers)


def serialize_context_pricing_tiers(tiers: tuple[ContextPricingTier, ...]) -> list[dict]:
    return [asdict(tier) for tier in tiers]


def effective_token_prices(
    input_tokens: int,
    input_price: float,
    output_price: float,
    cache_read_price: float,
    cache_write_price: float,
    tiers: tuple[ContextPricingTier, ...] = (),
) -> dict[str, Any]:
    """Select one tier using total input INCLUDING cache; never marginal slices.

    Tier multipliers are relative to the standard rates, not compounded. Output
    tokens do not participate in threshold selection. All prices are cents/M.
    """
    selected = None
    for tier in tiers:
        if input_tokens > tier.above_input_tokens:
            if selected is None or tier.above_input_tokens > selected.above_input_tokens:
                selected = tier
    input_multiplier = selected.input_multiplier if selected else 1.0
    output_multiplier = selected.output_multiplier if selected else 1.0
    return {
        "basis": "whole_request",
        "tier": "long_context" if selected else "standard",
        "above_input_tokens": selected.above_input_tokens if selected else None,
        "input_multiplier": input_multiplier,
        "output_multiplier": output_multiplier,
        "input_per_million_cents": input_price * input_multiplier,
        "output_per_million_cents": output_price * output_multiplier,
        "cache_read_per_million_cents": cache_read_price * input_multiplier,
        "cache_write_per_million_cents": cache_write_price * input_multiplier,
    }
