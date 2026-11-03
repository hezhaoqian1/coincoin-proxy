"""Per-request telemetry for request logs: first-token latency, stream flag, client metadata.

First token follows sub2api's default "semantic" rule: the first SSE event sent to the
client that is not protocol preamble (``response.created``/``in_progress``/...,
Anthropic ``message_start``/``ping``) or an error. Chat chunks count once a choice carries
content, reasoning or a tool call, so an early role-only chunk does not.

It is measured on the bytes sent to the client, so it covers every streaming path
(native, protocol-converted, fallback) and includes any pre-output channel failover:
it is the latency the client saw, from request arrival.
"""
from __future__ import annotations

import contextvars
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple

METADATA_EVENTS = frozenset({
    "response.created", "response.queued", "response.in_progress", "response.metadata",
    "keepalive", "message_start", "ping",
})
ERROR_EVENTS = frozenset({"error", "response.failed"})
CHAT_OUTPUT_DELTA_KEYS = ("content", "reasoning_content", "reasoning", "refusal", "tool_calls", "function_call")
# Preamble events can embed instructions/tools; give up rather than buffer without bound.
MAX_SCAN_BYTES = 1 << 20

USER_AGENT_MAX = 255
CLIENT_IP_MAX = 64
REASONING_EFFORT_MAX = 16


@dataclass
class RequestTelemetry:
    started_at: float
    user_agent: str = ""
    client_ip: str = ""
    stream: Optional[bool] = None
    reasoning_effort: str = ""
    first_token_at: Optional[float] = None


_current: contextvars.ContextVar[Optional[RequestTelemetry]] = contextvars.ContextVar(
    "coincoin_request_telemetry", default=None
)


def current_request_telemetry() -> Optional[RequestTelemetry]:
    return _current.get()


def note_request_payload(payload: Any) -> None:
    """Record the client's stream flag and reasoning effort once the body is parsed."""
    telemetry = _current.get()
    if telemetry is None or not isinstance(payload, dict):
        return
    telemetry.stream = bool(payload.get("stream"))
    effort = payload.get("reasoning_effort")
    for container in ("reasoning", "output_config"):  # Responses API, Anthropic Messages
        nested = payload.get(container)
        if not effort and isinstance(nested, dict):
            effort = nested.get("effort")
    if isinstance(effort, str):
        telemetry.reasoning_effort = effort.strip()[:REASONING_EFFORT_MAX]


def request_log_fields(duration_ms: int = 0) -> Dict[str, Any]:
    """Columns for the current request's log row; empty outside a request."""
    telemetry = _current.get()
    if telemetry is None:
        return {}
    first_token_ms = None
    if telemetry.first_token_at is not None:
        first_token_ms = max(0, int((telemetry.first_token_at - telemetry.started_at) * 1000))
        # duration_ms starts after auth inside the handler; never show first token past it.
        if duration_ms and duration_ms > 0:
            first_token_ms = min(first_token_ms, int(duration_ms))
    return {
        "first_token_ms": first_token_ms,
        "is_stream": bool(telemetry.stream),
        "reasoning_effort": telemetry.reasoning_effort,
        "user_agent": telemetry.user_agent,
        "client_ip": telemetry.client_ip,
    }


def is_first_token_event(event_type: Optional[str], data: bytes) -> bool:
    text = data.strip()
    if text == b"[DONE]":
        return False
    if event_type:
        return event_type not in METADATA_EVENTS and event_type not in ERROR_EVENTS
    if not text:
        return False
    try:
        obj = json.loads(text)
    except ValueError:
        return True
    if not isinstance(obj, dict):
        return True
    payload_type = obj.get("type")
    if isinstance(payload_type, str) and payload_type:
        return payload_type not in METADATA_EVENTS and payload_type not in ERROR_EVENTS
    if "error" in obj:
        return False
    choices = obj.get("choices")
    if isinstance(choices, list):
        return any(_chat_choice_has_output(choice) for choice in choices)
    return True


def _chat_choice_has_output(choice: Any) -> bool:
    if not isinstance(choice, dict):
        return False
    if choice.get("text"):
        return True
    delta = choice.get("delta")
    return isinstance(delta, dict) and any(delta.get(key) for key in CHAT_OUTPUT_DELTA_KEYS)


class FirstTokenScanner:
    """Incremental SSE parser that stops at the first output event."""

    __slots__ = ("done", "_pending", "_event", "_data", "_scanned")

    def __init__(self) -> None:
        self.done = False
        self._pending = b""
        self._event: Optional[str] = None
        self._data: list[bytes] = []
        self._scanned = 0

    def feed(self, chunk: bytes) -> bool:
        """Return True exactly once: when ``chunk`` completes the first output event."""
        if self.done or not chunk:
            return False
        self._scanned += len(chunk)
        if self._scanned > MAX_SCAN_BYTES:
            self.done = True
            return False
        lines = (self._pending + chunk).split(b"\n")
        self._pending = lines.pop()
        for raw in lines:
            line = raw.rstrip(b"\r")
            if not line:
                event, data = self._event, b"\n".join(self._data)
                self._event, self._data = None, []
                if (event or data) and is_first_token_event(event, data):
                    self.done = True
                    return True
                continue
            if line.startswith(b":"):
                continue
            name, _, value = line.partition(b":")
            if value.startswith(b" "):
                value = value[1:]
            if name == b"event":
                event = value.decode("utf-8", "replace").strip()
                # "message" is the SSE default type: classify such events by their data.
                self._event = event if event and event != "message" else None
            elif name == b"data" and self._event is None:
                # A named event is classified by its name alone; keep data only when needed.
                self._data.append(value)
        return False


def _header_text(value: bytes, limit: int) -> str:
    text = value.decode("latin-1", "replace")
    return "".join(ch for ch in text if ch.isprintable()).strip()[:limit]


def _client_metadata(scope: Dict[str, Any]) -> Tuple[str, str]:
    headers: Iterable[Tuple[bytes, bytes]] = scope.get("headers") or ()
    user_agent = forwarded = real_ip = b""
    for name, value in headers:
        if name == b"user-agent":
            user_agent = value
        elif name == b"x-forwarded-for":
            forwarded = value
        elif name == b"x-real-ip":
            real_ip = value
    # Same precedence as proxy._client_ip.
    if forwarded:
        ip = _header_text(forwarded.split(b",")[0], CLIENT_IP_MAX)
    elif real_ip:
        ip = _header_text(real_ip, CLIENT_IP_MAX)
    else:
        client = scope.get("client")
        ip = str(client[0])[:CLIENT_IP_MAX] if client else ""
    return _header_text(user_agent, USER_AGENT_MAX), ip


def _is_event_stream(headers: Iterable[Tuple[bytes, bytes]]) -> bool:
    for name, value in headers or ():
        if name.lower() == b"content-type":
            return value.split(b";")[0].strip().lower() == b"text/event-stream"
    return False


class RequestTelemetryASGIMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[..., Awaitable[Dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        user_agent, client_ip = _client_metadata(scope)
        telemetry = RequestTelemetry(started_at=time.monotonic(), user_agent=user_agent, client_ip=client_ip)
        token = _current.set(telemetry)
        scanner: Optional[FirstTokenScanner] = None

        async def send_wrapper(message: Dict[str, Any]) -> None:
            nonlocal scanner
            message_type = message.get("type")
            if message_type == "http.response.start":
                if _is_event_stream(message.get("headers")):
                    scanner = FirstTokenScanner()
                    if telemetry.stream is None:
                        telemetry.stream = True
            elif message_type == "http.response.body" and scanner is not None and not scanner.done:
                if scanner.feed(message.get("body") or b""):
                    telemetry.first_token_at = time.monotonic()
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _current.reset(token)
