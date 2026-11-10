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
from typing import Any, Iterable, Tuple

_MAX_SCAN_CHARS = 64 * 1024

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
