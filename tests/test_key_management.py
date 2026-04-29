import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.keys as keys_module


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class _FakeDB:
    def __init__(self, execute_results=None):
        self.execute_results = list(execute_results or [])
        self.added = []
        self.commits = 0

    async def execute(self, _query):
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return self.execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class DeveloperKeyManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_active_key_summary_for_console_session(self):
        user = SimpleNamespace(id="u_123")
        created_at = datetime(2026, 4, 29, 12, 0, 0)
        key_row = SimpleNamespace(
            id="k_123",
            encrypted_key="encrypted-payload",
            created_at=created_at,
            last_used_at=None,
            status="active",
        )
        db = _FakeDB(
            execute_results=[
                _ScalarResult(1),
                _ScalarResult(key_row),
            ]
        )
        request = SimpleNamespace()

        with patch.object(keys_module, "authenticate_user", AsyncMock(return_value=user)), patch(
            "app.security.decrypt_api_key", return_value="sk_cc_abcdefghijklmnopqrstuvwxyz1234"
        ):
            result = await keys_module.get_my_developer_key_state(request, db)

        self.assertTrue(result.has_active_key)
        self.assertEqual(result.active_key_count, 1)
        self.assertIsNotNone(result.latest_key)
        self.assertEqual(result.latest_key.key_id, "k_123")
        self.assertEqual(result.latest_key.masked_key, "sk_cc_ab...1234")
        self.assertEqual(result.latest_key.created_at, created_at)

    async def test_returns_empty_state_when_user_has_no_active_api_keys(self):
        user = SimpleNamespace(id="u_456")
        db = _FakeDB(
            execute_results=[
                _ScalarResult(0),
                _ScalarResult(None),
            ]
        )
        request = SimpleNamespace()

        with patch.object(keys_module, "authenticate_user", AsyncMock(return_value=user)):
            result = await keys_module.get_my_developer_key_state(request, db)

        self.assertFalse(result.has_active_key)
        self.assertEqual(result.active_key_count, 0)
        self.assertIsNone(result.latest_key)

    async def test_lists_multiple_developer_keys(self):
        user = SimpleNamespace(id="u_list")
        keys = [
            SimpleNamespace(
                id="k_new",
                encrypted_key="enc-new",
                status="active",
                created_at=datetime(2026, 4, 29, 12, 0, 0),
                last_used_at=datetime(2026, 4, 29, 13, 0, 0),
            ),
            SimpleNamespace(
                id="k_old",
                encrypted_key="enc-old",
                status="disabled",
                created_at=datetime(2026, 4, 20, 12, 0, 0),
                last_used_at=None,
            ),
        ]
        db = _FakeDB(execute_results=[_ScalarResult(keys)])
        request = SimpleNamespace()

        with patch.object(keys_module, "authenticate_user", AsyncMock(return_value=user)), patch(
            "app.security.decrypt_api_key",
            side_effect=[
                "sk_cc_newabcdefghijklmnopqrstuvwxyz1234",
                "sk_cc_oldabcdefghijklmnopqrstuvwxyz5678",
            ],
        ):
            result = await keys_module.list_my_developer_keys(request, db)

        self.assertEqual(result.total, 2)
        self.assertEqual(result.active, 1)
        self.assertEqual(result.disabled, 1)
        self.assertEqual(result.data[0].key_id, "k_new")
        self.assertEqual(result.data[1].status, "disabled")

    async def test_creates_new_developer_key(self):
        user = SimpleNamespace(id="u_create")
        db = _FakeDB()
        request = SimpleNamespace()

        with patch.object(keys_module, "authenticate_user", AsyncMock(return_value=user)), patch.object(
            keys_module, "generate_api_key", return_value="sk_cc_createdabcdefghijklmnopqrstuvwxyz9999"
        ), patch.object(keys_module, "generate_id", return_value="k_created"), patch.object(
            keys_module, "hash_key", return_value="hashed-key"
        ), patch.object(
            keys_module, "encrypt_api_key", return_value="encrypted-created"
        ):
            result = await keys_module.create_my_developer_key(request, db)

        self.assertEqual(result.key_id, "k_created")
        self.assertTrue(result.api_key.startswith("sk_cc_created"))
        self.assertEqual(result.masked_key, "sk_cc_cr...9999")
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].kind, "api")
        self.assertEqual(db.commits, 1)

    async def test_updates_existing_developer_key_status(self):
        user = SimpleNamespace(id="u_update")
        key_row = SimpleNamespace(
            id="k_disable",
            user_id="u_update",
            kind="api",
            encrypted_key="enc-disable",
            status="active",
            created_at=datetime(2026, 4, 28, 10, 0, 0),
            last_used_at=None,
        )
        db = _FakeDB(execute_results=[_ScalarResult(key_row)])
        request = SimpleNamespace()
        payload = keys_module.DeveloperKeyUpdateRequest(status="disabled")

        with patch.object(keys_module, "authenticate_user", AsyncMock(return_value=user)), patch(
            "app.security.decrypt_api_key", return_value="sk_cc_disableabcdefghijklmnopqrstuvwxyz7777"
        ):
            result = await keys_module.update_my_developer_key("k_disable", payload, request, db)

        self.assertEqual(result.key_id, "k_disable")
        self.assertEqual(result.status, "disabled")
        self.assertEqual(key_row.status, "disabled")
        self.assertEqual(db.commits, 1)
