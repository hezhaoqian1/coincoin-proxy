"""Tolerate the two most common base-URL mistakes in client configuration.

Clients build request URLs as ``<base URL> + <endpoint path>``, and whether the
endpoint path already carries ``/v1`` depends on the SDK:

* Anthropic SDKs (Claude Code, Claude Desktop) append ``/v1/messages``, so a
  base URL ending in ``/v1`` produces ``/v1/v1/messages``.
* OpenAI SDKs append ``/chat/completions``, so a base URL without ``/v1``
  produces ``/chat/completions``.

Neither path has a route, so both used to fall through to the SPA's GET-only
catch-all and come back as ``405 Method Not Allowed`` (or the index page for a
GET). This middleware rewrites such paths to the canonical endpoint before
routing. It only touches paths that have no route of their own, so every path
that works today keeps working unchanged.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any


logger = logging.getLogger("coincoin.api_path_compat")

ASGIApp = Callable[
    [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
    Awaitable[None],
]

# `/v1/v1/...` and `/openai/v1/v1/...` (a `/v1` base URL plus an SDK that adds
# `/v1` itself). A doubled version segment is never a real route or SPA page.
_REPEATED_VERSION = re.compile(r"^(?P<ns>/openai)?(?:/v1){2,}(?=/|$)")

# Inference endpoints that live under `/v1` (and, except for the Anthropic
# ones, also under `/openai/v1`). Kept to API-only names so a GET for a website
# page such as `/usage` or `/dashboard` is never turned into a JSON API call.
_UNVERSIONED_ENDPOINT = re.compile(
    r"^(?P<ns>/openai)?/(?P<endpoint>"
    r"messages(?:/count_tokens)?"
    r"|chat/completions"
    r"|embeddings"
    r"|models(?:/[^/]+)?"
    r"|responses"
    r"|images/(?:generations|edits)"
    r"|image-jobs/[^/]+"
    r"|videos/generations(?:/[^/]+)?"
    r")$"
)


def _canonical_prefix(path: str) -> tuple[str, str] | None:
    """Return ``(old_prefix, new_prefix)`` to swap at the start of ``path``.

    Only the leading part of the path ever changes, so the same swap can be
    applied to ``raw_path`` without re-encoding the rest of it.
    """
    leading = len(path) - len(path.lstrip("/"))
    if leading > 1:
        # A base URL with a trailing slash: `https://host/` + `/v1/messages`.
        rest = _canonical_prefix("/" + path[leading:])
        if rest is None:
            return path[:leading], "/"
        old_prefix, new_prefix = rest
        return path[:leading] + old_prefix[1:], new_prefix

    match = _REPEATED_VERSION.match(path)
    if match:
        return match.group(0), f"{match.group('ns') or ''}/v1"

    match = _UNVERSIONED_ENDPOINT.match(path)
    if match:
        ns = match.group("ns") or ""
        endpoint = match.group("endpoint")
        if not ns and endpoint == "responses":
            return None  # `/responses` already has its own route.
        if ns and endpoint.startswith("messages"):
            return None  # Anthropic endpoints only exist under `/v1`.
        # Swap `<ns>/` for `<ns>/v1/` so the old prefix is never empty.
        return f"{ns}/", f"{ns}/v1/"

    return None


def normalize_api_path(path: str) -> str:
    """Return the canonical form of ``path`` (``path`` itself if it is fine)."""
    swap = _canonical_prefix(path)
    if swap is None:
        return path
    old_prefix, new_prefix = swap
    return new_prefix + path[len(old_prefix):]


class ApiPathCompatASGIMiddleware:
    """Rewrite mis-prefixed API paths (`/v1/v1/...`, `/chat/completions`, ...)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        swap = _canonical_prefix(path)
        if swap is None:
            await self.app(scope, receive, send)
            return

        old_prefix, new_prefix = swap
        new_path = new_prefix + path[len(old_prefix):]
        scope = dict(scope)
        scope["path"] = new_path
        raw_path = scope.get("raw_path")
        if isinstance(raw_path, (bytes, bytearray)):
            old_raw_prefix = old_prefix.encode("ascii")
            if raw_path.startswith(old_raw_prefix):
                scope["raw_path"] = new_prefix.encode("ascii") + bytes(raw_path[len(old_raw_prefix):])
            else:
                scope["raw_path"] = new_path.encode("utf-8")
        logger.debug("rewrote api path method=%s from=%s to=%s", scope.get("method"), path, new_path)
        await self.app(scope, receive, send)
