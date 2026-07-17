import logging
import time
from typing import Any, Awaitable, Callable


ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[dict]], Callable[..., Awaitable[None]]], Awaitable[None]]


class AdminTimingASGIMiddleware:
    """Expose and log server processing time for admin HTTP requests only."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        slow_threshold_ms: float = 1000,
        logger: logging.Logger | None = None,
    ) -> None:
        self.app = app
        self.slow_threshold_ms = max(0.0, float(slow_threshold_ms))
        self.logger = logger or logging.getLogger("coincoin.admin.performance")

    async def __call__(self, scope, receive, send) -> None:
        path = str(scope.get("path") or "")
        if scope.get("type") != "http" or not (path == "/admin" or path.startswith("/admin/")):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        method = str(scope.get("method") or "")

        async def send_wrapper(message) -> None:
            if message.get("type") == "http.response.start":
                duration_ms = (time.perf_counter() - started) * 1000
                status_code = int(message.get("status") or 0)
                headers = list(message.get("headers") or [])
                duration_text = f"{duration_ms:.2f}".encode("ascii")
                headers.append((b"x-process-time-ms", duration_text))
                headers.append((b"server-timing", b"app;dur=" + duration_text))
                message = {**message, "headers": headers}
                if duration_ms >= self.slow_threshold_ms:
                    self.logger.warning(
                        "slow admin request method=%s path=%s status=%s duration_ms=%.2f",
                        method,
                        path,
                        status_code,
                        duration_ms,
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)
