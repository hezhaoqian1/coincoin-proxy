import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .config import settings


logger = logging.getLogger("coincoin.image_keepalive")

_IMAGE_SYNC_PATHS = frozenset(
    {
        "/v1/images/generations",
        "/v1/images/edits",
        "/openai/v1/images/generations",
        "/openai/v1/images/edits",
    }
)

ASGIApp = Callable[
    [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
    Awaitable[None],
]


class ImageJSONKeepaliveASGIMiddleware:
    """Emit JSON-safe whitespace while synchronous image requests are pending."""

    def __init__(self, app: ASGIApp, interval_seconds: float | None = None) -> None:
        self.app = app
        self.interval_seconds = interval_seconds

    def _interval(self) -> float:
        value = self.interval_seconds
        if value is None:
            value = settings.image_nonstream_keepalive_interval_seconds
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        interval = self._interval()
        if (
            scope.get("type") != "http"
            or str(scope.get("method") or "").upper() != "POST"
            or scope.get("path") not in _IMAGE_SYNC_PATHS
            or interval <= 0
        ):
            await self.app(scope, receive, send)
            return

        captured: list[dict[str, Any]] = []

        async def capture_send(message: dict[str, Any]) -> None:
            copied = dict(message)
            if "headers" in copied:
                copied["headers"] = list(copied["headers"])
            captured.append(copied)

        downstream = asyncio.create_task(self.app(scope, receive, capture_send))
        committed = False
        try:
            done, _ = await asyncio.wait({downstream}, timeout=interval)
            if downstream in done:
                downstream.result()
                await self._forward_captured(captured, send)
                return

            if downstream.done():
                downstream.result()
                await self._forward_captured(captured, send)
                return

            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"cache-control", b"no-cache, no-store, no-transform"),
                        (b"x-accel-buffering", b"no"),
                        (b"x-coincoin-image-keepalive", b"whitespace"),
                    ],
                }
            )
            committed = True
            await send({"type": "http.response.body", "body": b" \n", "more_body": True})

            while not downstream.done():
                done, _ = await asyncio.wait({downstream}, timeout=interval)
                if downstream in done:
                    break
                await send({"type": "http.response.body", "body": b" \n", "more_body": True})

            try:
                downstream.result()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("image request failed after JSON keepalive committed")
                await self._send_late_exception(send)
                return

            await self._forward_committed_body(captured, send)
        finally:
            if not downstream.done():
                downstream.cancel()
                await asyncio.gather(downstream, return_exceptions=True)
            elif not committed:
                # Retrieve a completed exception when forwarding itself failed.
                try:
                    downstream.exception()
                except asyncio.CancelledError:
                    pass

    @staticmethod
    async def _forward_captured(
        captured: list[dict[str, Any]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        for message in captured:
            await send(message)

    @staticmethod
    async def _forward_committed_body(
        captured: list[dict[str, Any]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        body_messages = [message for message in captured if message.get("type") == "http.response.body"]
        if not body_messages:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        for message in body_messages:
            await send(message)

        if body_messages[-1].get("more_body", False):
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    @staticmethod
    async def _send_late_exception(send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        payload = {
            "error": {
                "message": "Image request failed after the response keepalive started.",
                "type": "server_error",
                "code": "image_keepalive_internal_error",
            }
        }
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                "more_body": False,
            }
        )
