"""Classify upstream errors that describe the request itself, not the channel.

A prompt that exceeds the model's context window fails the same way on every
provider channel, so failing over only multiplies latency (each hop has to
upload and tokenize the whole prompt again).  Some relays also wrap this error
in a 5xx status, so the decision has to come from the error body rather than
the status code.  The matching rules mirror sub2api's
``isOpenAIContextWindowError``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Tuple

_MAX_SCAN_CHARS = 64 * 1024

# Upstream account has run out of money/quota.  Substring match on the
# lower-cased error body; shared by the client-facing error rewrite in
# ``proxy.py`` and the channel balance alert in ``channel_monitoring.py``.
INSUFFICIENT_BALANCE_PATTERNS: Tuple[str, ...] = (
    "用户额度不足",
    "额度不足",
    "余额不足",
    "剩余额度",
    "最低保留额度",
    "额度已用尽",
    "预扣费额度失败",
    "credit balance is too low",
    "quota is not enough",
    "billing hard limit",
    "exceeded your current quota",
)

# "insufficient balance", "INSUFFICIENT_BALANCE", "Insufficient user quota"
# (zzshu), "insufficient account balance" (sub2api), "insufficient_quota"
# (OpenAI), "Insufficient credits" (OpenRouter), ...
_INSUFFICIENT_FUNDS_RE = re.compile(
    r"insufficient[\s_-]+(?:(?:user|account)[\s_-]+)?(?:balance|quota|credits?|funds)"
)
_SECRET_RE = re.compile(r"\b(?:sk|ak)[-_][A-Za-z0-9_\-]{10,}\b")


def _error_search_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[: _MAX_SCAN_CHARS * 4]).decode("utf-8", errors="replace")[:_MAX_SCAN_CHARS]
    if isinstance(value, str):
        return value[:_MAX_SCAN_CHARS]
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:_MAX_SCAN_CHARS]
    except (TypeError, ValueError):
        return str(value)[:_MAX_SCAN_CHARS]


def is_insufficient_balance_error(body: Any) -> bool:
    """Return True when an upstream error body says the account is out of balance/quota."""
    text = _error_search_text(body).lower()
    if not text:
        return False
    return bool(_INSUFFICIENT_FUNDS_RE.search(text)) or any(
        pattern in text for pattern in INSUFFICIENT_BALANCE_PATTERNS
    )


def upstream_error_summary(body: Any, *, max_length: int = 200) -> str:
    """Short single-line description of an upstream error body for operator alerts."""
    message: Any = None
    payload: Any = body
    if isinstance(payload, (bytes, bytearray)):
        payload = _error_search_text(payload)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            message = payload
    if message is None and isinstance(payload, dict):
        for path in (("error", "message"), ("message",), ("msg",), ("detail",), ("error",)):
            value = _lookup(payload, path)
            if isinstance(value, str) and value.strip():
                message = value
                break
        if message is None:
            message = _error_search_text(payload)
    if message is None:
        message = _error_search_text(payload)
    text = " ".join(str(message or "").split())
    text = _SECRET_RE.sub("[redacted]", text)
    return text[:max_length]

# Only structured error fields are inspected for JSON bodies so that request
# content echoed back inside an error object cannot change the classification.
_STRUCTURED_ERROR_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("error", "message"),
    ("error", "code"),
    ("response", "error", "message"),
    ("response", "error", "code"),
    ("message",),
    ("code",),
    ("detail",),
    ("error",),
)


def _matches_context_window_text(text: Any) -> bool:
    lower = str(text or "").strip().lower()
    if not lower:
        return False
    if "context_length_exceeded" in lower or "context_too_large" in lower:
        return True
    if "prompt is too long" in lower or "input is too long" in lower:
        return True
    if "maximum context length" in lower or "max context length" in lower:
        return True
    exceeded = "exceed" in lower or "too large" in lower or "too long" in lower
    # "context limit": kedaya ("The request exceeds the context limit.").
    if exceeded and ("context window" in lower or "context length" in lower or "context limit" in lower):
        return True
    return exceeded and "token limit" in lower and "context" in lower


def _lookup(payload: Any, path: Iterable[str]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def is_context_window_error(body: Any) -> bool:
    """Return True when an upstream error body reports a context overflow.

    ``body`` may be raw bytes/str from the upstream or an already-decoded
    JSON object.
    """
    if body is None:
        return False
    payload: Any = body
    if isinstance(body, (bytes, bytearray)):
        payload = bytes(body[: _MAX_SCAN_CHARS * 4]).decode("utf-8", errors="replace")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return False
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return _matches_context_window_text(text[:_MAX_SCAN_CHARS])
    if isinstance(payload, dict):
        for path in _STRUCTURED_ERROR_PATHS:
            value = _lookup(payload, path)
            if isinstance(value, (str, int)) and _matches_context_window_text(value):
                return True
        return False
    if isinstance(payload, str):
        return _matches_context_window_text(payload[:_MAX_SCAN_CHARS])
    return False
