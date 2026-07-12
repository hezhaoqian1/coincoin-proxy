import unittest
from datetime import datetime, timedelta
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
    def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/embeddings",
                "headers": headers or [(b"authorization", b"Bearer sk_cc_test")],
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
        ), patch(
            "app.billing.get_available_balance_cents",
            AsyncMock(return_value={"available_cents": 100}),
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

    async def test_authenticate_user_refreshes_console_session(self) -> None:
        request = self._request()
        old_expires_at = datetime.utcnow() + timedelta(days=1)
        session_key = SimpleNamespace(
            id="k_session",
            user=SimpleNamespace(id="u_test", status="active"),
            user_id="u_test",
            kind="session",
            status="active",
            expires_at=old_expires_at,
            monthly_quota_cents=None,
            total_quota_cents=None,
            ip_allowlist=None,
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=_ScalarResult(session_key)),
            commit=AsyncMock(),
        )

        with patch.object(proxy_module.model_registry, "ensure_initialized"), patch.object(
            proxy_module.model_registry, "has_routable_models", return_value=True
        ), patch.object(
            proxy_module.key_cache, "get", AsyncMock(return_value=None)
        ), patch.object(
            proxy_module.key_cache, "set", AsyncMock()
        ), patch.object(
            proxy_module, "hash_key", return_value="hashed-session"
        ):
            user = await proxy_module.authenticate_user(request, db)

        self.assertEqual(user.id, "u_test")
        self.assertGreater(session_key.expires_at, old_expires_at + timedelta(days=20))
        db.commit.assert_awaited_once()

    async def test_authenticate_user_restores_user_model_override_snapshots_from_cache_hit(self) -> None:
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
                    proxy_module._KEY_ID_ATTR: "k_test",
                    proxy_module._KEY_KIND_ATTR: "api",
                    "controls": {},
                    "station_context": {},
                    "model_routing_overrides": {
                        "claude-opus-4-7": {
                            "provider_model": "gpt-5.5",
                            "upstream_model": "gpt-5.5",
                            "enabled": True,
                        }
                    },
                    "model_pricing_overrides": {
                        "claude-opus-4-7": {
                            "cache_read_multiplier_override": 1.0,
                        }
                    },
                }
            ),
        ):
            user = await proxy_module.authenticate_user(request, db)

        self.assertEqual(getattr(user, proxy_module._KEY_ID_ATTR), "k_test")
        self.assertEqual(
            getattr(user, "_model_routing_overrides")["claude-opus-4-7"]["upstream_model"],
            "gpt-5.5",
        )
        self.assertEqual(
            getattr(user, "_model_pricing_overrides")["claude-opus-4-7"]["cache_read_multiplier_override"],
            1.0,
        )

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

    async def test_authorize_request_still_rejects_console_session(self) -> None:
        request = self._request()
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=_ScalarResult(
                    SimpleNamespace(
                        id="u_test",
                        status="active",
                        balance=1000,
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
                    proxy_module._KEY_ID_ATTR: "k_session",
                    proxy_module._KEY_KIND_ATTR: "session",
                    "controls": {},
                }
            ),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await proxy_module.authorize_request(request, db)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "please generate an API key from your dashboard")

    async def test_authorize_workbench_request_delegates_console_session_to_active_api_key(self) -> None:
        request = self._request(
            [
                (b"authorization", b"Bearer sk_cc_session"),
                (b"x-coincoin-workbench", b"1"),
            ]
        )
        user = SimpleNamespace(
            id="u_test",
            status="active",
            balance=1000,
            token_limit=None,
            token_used=0,
            request_limit_per_minute=None,
            request_limit_per_day=None,
        )
        developer_key = SimpleNamespace(
            id="k_api",
            monthly_quota_cents=None,
            total_quota_cents=None,
            ip_allowlist=None,
            expires_at=None,
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _ScalarResult(user),
                    _ScalarResult(developer_key),
                    _ScalarResult(None),
                ]
            ),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )

        with patch.object(proxy_module.model_registry, "ensure_initialized"), patch.object(
            proxy_module.model_registry, "has_routable_models", return_value=True
        ), patch.object(
            proxy_module.key_cache,
            "get",
            AsyncMock(
                return_value={
                    "id": "u_test",
                    proxy_module._KEY_ID_ATTR: "k_session",
                    proxy_module._KEY_KIND_ATTR: "session",
                    "controls": {},
                }
            ),
        ), patch.object(
            proxy_module.usage_buffer, "get_pending_tokens", AsyncMock(return_value=0)
        ), patch.object(
            proxy_module.usage_buffer, "get_pending_cost", AsyncMock(return_value=0)
        ), patch("app.billing.get_available_balance_cents", AsyncMock(return_value={"available_cents": 1000})):
            authorized = await proxy_module.authorize_workbench_request(request, db)

        self.assertEqual(authorized.id, "u_test")
        self.assertEqual(getattr(authorized, proxy_module._KEY_KIND_ATTR), "api")
        self.assertEqual(getattr(authorized, proxy_module._KEY_ID_ATTR), "k_api")
        self.assertEqual(db.commit.await_count, 1)

    async def test_authorize_workbench_request_rejects_console_session_without_workbench_header(self) -> None:
        request = self._request([(b"authorization", b"Bearer sk_cc_session")])
        user = SimpleNamespace(
            id="u_test",
            status="active",
            balance=1000,
            token_limit=None,
            token_used=0,
            request_limit_per_minute=None,
            request_limit_per_day=None,
        )
        db = SimpleNamespace(execute=AsyncMock(return_value=_ScalarResult(user)))

        with patch.object(proxy_module.model_registry, "ensure_initialized"), patch.object(
            proxy_module.model_registry, "has_routable_models", return_value=True
        ), patch.object(
            proxy_module.key_cache,
            "get",
            AsyncMock(
                return_value={
                    "id": "u_test",
                    proxy_module._KEY_ID_ATTR: "k_session",
                    proxy_module._KEY_KIND_ATTR: "session",
                    "controls": {},
                }
            ),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await proxy_module.authorize_workbench_request(request, db)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "please generate an API key from your dashboard")
