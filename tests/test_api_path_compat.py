import unittest

from starlette.routing import Match

from app.api_path_compat import ApiPathCompatASGIMiddleware, normalize_api_path


def _scope(path: str, method: str = "POST", raw_path: bytes | None = None) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8") if raw_path is None else raw_path,
        "query_string": b"beta=true",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }


# (requested path, method, normalized path, route template it must hit)
_REWRITES = [
    # Anthropic SDK with a base URL that already ends in /v1 (Claude Desktop, Claude Code).
    ("/v1/v1/messages", "POST", "/v1/messages", "/v1/messages"),
    ("/v1/v1/messages/count_tokens", "POST", "/v1/messages/count_tokens", "/v1/messages/count_tokens"),
    ("/v1/v1/models", "GET", "/v1/models", "/v1/models"),
    ("/v1/v1/v1/messages", "POST", "/v1/messages", "/v1/messages"),
    ("/v1/v1/chat/completions", "POST", "/v1/chat/completions", "/v1/chat/completions"),
    ("/openai/v1/v1/responses", "POST", "/openai/v1/responses", "/openai/v1/responses"),
    # OpenAI SDK with a base URL that lacks /v1.
    ("/chat/completions", "POST", "/v1/chat/completions", "/v1/chat/completions"),
    ("/messages", "POST", "/v1/messages", "/v1/messages"),
    ("/messages/count_tokens", "POST", "/v1/messages/count_tokens", "/v1/messages/count_tokens"),
    ("/models", "GET", "/v1/models", "/v1/models"),
    ("/models/gpt-5.6", "GET", "/v1/models/gpt-5.6", "/v1/models/{model_id}"),
    ("/embeddings", "POST", "/v1/embeddings", "/v1/embeddings"),
    ("/images/generations", "POST", "/v1/images/generations", "/v1/images/generations"),
    ("/images/edits", "POST", "/v1/images/edits", "/v1/images/edits"),
    ("/image-jobs/generations", "POST", "/v1/image-jobs/generations", "/v1/image-jobs/generations"),
    ("/image-jobs/job_123", "GET", "/v1/image-jobs/job_123", "/v1/image-jobs/{job_id}"),
    ("/videos/generations", "POST", "/v1/videos/generations", "/v1/videos/generations"),
    ("/videos/generations/vid_123", "GET", "/v1/videos/generations/vid_123", "/v1/videos/generations/{job_id}"),
    ("/openai/chat/completions", "POST", "/openai/v1/chat/completions", "/openai/v1/chat/completions"),
    ("/openai/responses", "POST", "/openai/v1/responses", "/openai/v1/responses"),
    ("/openai/models", "GET", "/openai/v1/models", "/openai/v1/models"),
    # Base URL with a trailing slash.
    ("//v1/messages", "POST", "/v1/messages", "/v1/messages"),
    ("//v1/v1/messages", "POST", "/v1/messages", "/v1/messages"),
    ("//chat/completions", "POST", "/v1/chat/completions", "/v1/chat/completions"),
]

_UNCHANGED = [
    "/v1/messages",
    "/v1/chat/completions",
    "/v1/models",
    "/openai/v1/responses",
    "/responses",
    "/health",
    "/v1",
    # Website pages must stay pages even where a same-named /v1 API exists.
    "/",
    "/usage",
    "/dashboard",
    "/recharge",
    "/docs",
    "/settings",
    "/station",
    "/admin",
    "/admin/users",
    "/assets/index.js",
    # Anthropic endpoints have no /openai/v1 variant; leave these alone.
    "/openai/messages",
    "/v1/messagesx",
    "/chat/completions/extra",
]


class NormalizeApiPathTests(unittest.TestCase):
    def test_rewrites_mis_prefixed_api_paths(self) -> None:
        for requested, _, normalized, _ in _REWRITES:
            with self.subTest(requested=requested):
                self.assertEqual(normalize_api_path(requested), normalized)

    def test_leaves_working_paths_and_pages_alone(self) -> None:
        for path in _UNCHANGED:
            with self.subTest(path=path):
                self.assertEqual(normalize_api_path(path), path)


class RewriteTargetsExistTests(unittest.TestCase):
    """Every rewrite must land on a real API route of the production app."""

    def test_rewritten_paths_hit_api_routes_not_the_spa_fallback(self) -> None:
        from app.main import app

        for requested, method, normalized, expected_route in _REWRITES:
            with self.subTest(requested=requested):
                scope = _scope(normalized, method=method)
                matched = [
                    route
                    for route in app.router.routes
                    if route.matches(scope)[0] == Match.FULL
                ]
                self.assertTrue(matched, f"no route for {method} {scope['path']}")
                self.assertEqual(matched[0].path, expected_route)

    def test_requested_paths_have_no_route_of_their_own(self) -> None:
        from app.main import app

        for requested, method, _, _ in _REWRITES:
            with self.subTest(requested=requested):
                scope = _scope(requested, method=method)
                matched = [
                    route.path
                    for route in app.router.routes
                    if route.matches(scope)[0] == Match.FULL
                ]
                # Only the SPA catch-all (if the web build is present) may match today.
                self.assertTrue(set(matched) <= {"/{full_path:path}"}, matched)


class ApiPathCompatMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _seen_scope(self, scope: dict) -> dict:
        seen: list[dict] = []

        async def app(inner_scope, receive, send):
            seen.append(inner_scope)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass

        await ApiPathCompatASGIMiddleware(app)(scope, receive, send)
        self.assertEqual(len(seen), 1)
        return seen[0]

    async def test_rewrites_path_and_raw_path_and_keeps_query(self) -> None:
        original = _scope("/v1/v1/messages")
        seen = await self._seen_scope(original)

        self.assertEqual(seen["path"], "/v1/messages")
        self.assertEqual(seen["raw_path"], b"/v1/messages")
        self.assertEqual(seen["query_string"], b"beta=true")
        self.assertEqual(original["path"], "/v1/v1/messages")  # caller's scope untouched

    async def test_raw_path_keeps_percent_encoding_after_the_prefix(self) -> None:
        seen = await self._seen_scope(
            _scope("/models/a b", method="GET", raw_path=b"/models/a%20b")
        )

        self.assertEqual(seen["path"], "/v1/models/a b")
        self.assertEqual(seen["raw_path"], b"/v1/models/a%20b")

    async def test_passes_through_unchanged_paths_as_is(self) -> None:
        original = _scope("/v1/messages")
        seen = await self._seen_scope(original)

        self.assertIs(seen, original)

    async def test_ignores_non_http_scopes(self) -> None:
        original = {"type": "lifespan"}
        seen = await self._seen_scope(original)

        self.assertIs(seen, original)


if __name__ == "__main__":
    unittest.main()
