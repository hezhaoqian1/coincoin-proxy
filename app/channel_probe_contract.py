"""Response-shape contract for provider-channel generation probes.

This module deliberately has no database, routing, or network ownership. It
only classifies one completed HTTP response so protocol-specific reasoning
blocks cannot be mistaken for an empty model response.
"""

from __future__ import annotations

from typing import Any

import httpx


def mask_probe_message(message: str) -> str:
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


def _positive_token_count(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _probe_output_tokens_reported(payload: dict[str, Any]) -> bool:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return False
    if (
        _positive_token_count(usage.get("output_tokens"))
        or _positive_token_count(usage.get("completion_tokens"))
    ):
        return True
    for details_key in ("output_tokens_details", "completion_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, dict) and _positive_token_count(details.get("reasoning_tokens")):
            return True
    return False


def _probe_reasoning_was_truncated(payload: Any, *, endpoint: str, channel_type: str) -> bool:
    if not isinstance(payload, dict):
        return False
    if channel_type == "anthropic_compatible":
        if (
            payload.get("type") != "message"
            or payload.get("role") != "assistant"
            or payload.get("stop_reason") != "max_tokens"
        ):
            return False
        content = payload.get("content")
        has_signed_reasoning = isinstance(content, list) and any(
            isinstance(item, dict)
            and (
                (
                    item.get("type") == "thinking"
                    and bool(str(item.get("thinking") or item.get("signature") or "").strip())
                )
                or (
                    item.get("type") == "redacted_thinking"
                    and bool(str(item.get("data") or "").strip())
                )
            )
            for item in content
        )
        return has_signed_reasoning and _probe_output_tokens_reported(payload)

    if endpoint == "chat/completions":
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return False
        truncated_choice = any(
            isinstance(choice, dict)
            and choice.get("finish_reason") == "length"
            and isinstance(choice.get("message"), dict)
            and choice["message"].get("role") == "assistant"
            and any(
                bool(str(choice["message"].get(key) or "").strip())
                for key in ("reasoning", "reasoning_content")
            )
            for choice in choices
        )
        return truncated_choice and _probe_output_tokens_reported(payload)

    incomplete_details = payload.get("incomplete_details")
    if payload.get("status") != "incomplete" or not isinstance(incomplete_details, dict):
        return False
    if incomplete_details.get("reason") not in {"max_output_tokens", "max_tokens"}:
        return False
    output = payload.get("output")
    has_reasoning_item = isinstance(output, list) and any(
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and (
            bool(str(item.get("id") or item.get("encrypted_content") or "").strip())
            or isinstance(item.get("summary"), list)
        )
        for item in output
    )
    return has_reasoning_item and _probe_output_tokens_reported(payload)


def classify_probe_response(
    response: httpx.Response,
    payload: Any,
    latency_ms: int,
    *,
    endpoint: str,
    channel_type: str,
) -> tuple[str, str]:
    """Return the monitor status/message for one provider probe response."""
    if response.status_code in {408, 409, 429} or response.status_code >= 500:
        return "failed", f"HTTP {response.status_code}"
    if response.status_code < 200 or response.status_code >= 300:
        return "error", f"HTTP {response.status_code}"
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        return "failed", mask_probe_message(message or f"HTTP {response.status_code}")
    if not _has_structured_model_output(payload, endpoint=endpoint, channel_type=channel_type):
        if _probe_reasoning_was_truncated(payload, endpoint=endpoint, channel_type=channel_type):
            return "degraded", "probe output truncated before visible text"
        return "failed", "response missing structured model output"
    if latency_ms >= 30_000:
        return "degraded", f"slow response {latency_ms}ms"
    return "operational", "ok"
