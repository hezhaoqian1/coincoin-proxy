import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
import app.proxy as proxy_module


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class ProxyAuthCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._billing_mode = settings.billing_mode
        settings.billing_mode = "balance"

    def tearDown(self) -> None:
        settings.billing_mode = self._billing_mode

    @staticmethod
    def _request() -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/embeddings",
                "headers": [(b"authorization", b"Bearer sk_cc_test")],
            }
        )

    async def test_authorize_request_uses_fresh_balance_on_cache_hit(self) -> None:
        request = self._request()
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=_ScalarResult(
                    SimpleNamespace(
                        id="u_test",
                        status="active",
                        balance=100,
                        token_limit=None,
                        token_used=0,
                        request_limit_per_minute=None,
                        request_limit_per_day=None,
                    )
                )
            )
        )

        with patch.object(proxy_module.model_registry, "ensure_initialized"), patch.object(
            proxy_module.model_registry, "has_routable_models", return_value=True
        ), patch.object(
            proxy_module.key_cache,
            "get",
            AsyncMock(
                return_value={
                    "id": "u_test",
                    "balance": 0,
                    "token_limit": None,
                    "token_used": 0,
                    "request_limit_per_minute": None,
                    "request_limit_per_day": None,
                    proxy_module._KEY_KIND_ATTR: "api",
                }
            ),
        ), patch.object(
            proxy_module.usage_buffer, "get_pending_tokens", AsyncMock(return_value=0)
        ), patch.object(
            proxy_module.usage_buffer, "get_pending_cost_for_api_key", AsyncMock(return_value=0)
        ), patch.object(
            proxy_module.usage_buffer, "get_pending_cost", AsyncMock(return_value=0)
        ):
            user = await proxy_module.authorize_request(request, db)

        self.assertEqual(user.id, "u_test")
        self.assertEqual(user.balance, 100)

    async def test_authorize_request_uses_fresh_token_usage_on_cache_hit(self) -> None:
        request = self._request()
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=_ScalarResult(
                    SimpleNamespace(
                        id="u_test",
                        status="active",
                        balance=100,
                        token_limit=100,
                        token_used=100,
                        request_limit_per_minute=None,
                        request_limit_per_day=None,
                    )
                )
            )
        )

        with patch.object(proxy_module.model_registry, "ensure_initialized"), patch.object(
            proxy_module.model_registry, "has_routable_models", return_value=True
        ), patch.object(
            proxy_module.key_cache,
            "get",
            AsyncMock(
                return_value={
                    "id": "u_test",
                    "balance": 100,
                    "token_limit": 100,
                    "token_used": 0,
                    "request_limit_per_minute": None,
                    "request_limit_per_day": None,
                    proxy_module._KEY_KIND_ATTR: "api",
                }
            ),
        ), patch.object(
            proxy_module.usage_buffer, "get_pending_tokens", AsyncMock(return_value=0)
        ), patch.object(
            proxy_module.usage_buffer, "get_pending_cost_for_api_key", AsyncMock(return_value=0)
        ), patch.object(
            proxy_module.usage_buffer, "get_pending_cost", AsyncMock(return_value=0)
        ):
            with self.assertRaises(HTTPException) as ctx:
                await proxy_module.authorize_request(request, db)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail, "token limit exceeded")

    async def test_authorize_request_rejects_ip_outside_key_allowlist(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/embeddings",
                "headers": [
                    (b"authorization", b"Bearer sk_cc_test"),
                    (b"x-forwarded-for", b"198.51.100.9"),
                ],
            }
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=_ScalarResult(
                    SimpleNamespace(
                        id="u_test",
                        status="active",
                        balance=100,
                        token_limit=None,
                        token_used=0,
                        request_limit_per_minute=None,
                        request_limit_per_day=None,
                    )
                )
            )
        )

        with patch.object(proxy_module.model_registry, "ensure_initialized"), patch.object(
            proxy_module.model_registry, "has_routable_models", return_value=True
        ), patch.object(
            proxy_module.key_cache,
            "get",
            AsyncMock(
                return_value={
                    "id": "u_test",
                    proxy_module._KEY_ID_ATTR: "k_test",
                    proxy_module._KEY_KIND_ATTR: "api",
                    "controls": {"ip_allowlist": '["203.0.113.0/24"]'},
                }
            ),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await proxy_module.authorize_request(request, db)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "api key ip not allowed")

    async def test_authorize_request_rejects_monthly_key_quota(self) -> None:
        request = self._request()
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _ScalarResult(
                        SimpleNamespace(
                            id="u_test",
                            status="active",
                            balance=10000,
                            token_limit=None,
                            token_used=0,
                            request_limit_per_minute=None,
                            request_limit_per_day=None,
                        )
                    ),
                    _ScalarResult(1000),
                ]
            )
        )

        with patch.object(proxy_module.model_registry, "ensure_initialized"), patch.object(
            proxy_module.model_registry, "has_routable_models", return_value=True
        ), patch.object(
            proxy_module.key_cache,
            "get",
            AsyncMock(
                return_value={
                    "id": "u_test",
                    proxy_module._KEY_ID_ATTR: "k_test",
                    proxy_module._KEY_KIND_ATTR: "api",
                    "controls": {"monthly_quota_cents": 1000},
                }
            ),
        ), patch.object(
            proxy_module.usage_buffer, "get_pending_cost_for_api_key", AsyncMock(return_value=0)
        ):
            with self.assertRaises(HTTPException) as ctx:
                await proxy_module.authorize_request(request, db)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail, "api key monthly quota exceeded")
