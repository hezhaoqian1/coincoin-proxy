import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app.auth as auth_module


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, execute_results=None):
        self.execute_results = list(execute_results or [])
        self.added = []
        self.flushes = 0
        self.commits = 0

    async def execute(self, _query):
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return self.execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


def _request(ip="127.0.0.1"):
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


def _session_request(key="sk_cc_session"):
    return SimpleNamespace(headers={"authorization": f"Bearer {key}"}, client=SimpleNamespace(host="127.0.0.1"))


class _BackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


class EmailVerificationAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_default_balance = auth_module.settings.default_balance
        auth_module.settings.default_balance = 0

    async def asyncTearDown(self):
        auth_module.settings.default_balance = self._original_default_balance

    async def test_register_send_code_returns_verification_session(self):
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(None),  # email owner
                _ScalarOneOrNoneResult(None),  # latest code for cooldown
            ]
        )
        payload = auth_module.AuthRegisterSendCodeRequest(email="Alice@Gmail.com")
        background_tasks = _BackgroundTasks()

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)):
            result = await auth_module.register_send_code(payload, _request(), background_tasks, db)

        self.assertEqual(result.email, "alice@gmail.com")
        self.assertEqual(result.status, "code_sent")
        self.assertTrue(result.verification_id.startswith("regv_"))
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(background_tasks.tasks), 1)
        verification = next(obj for obj in db.added if obj.__class__.__name__ == "EmailVerificationCode")
        self.assertEqual(verification.user_id, result.verification_id)
        self.assertEqual(verification.email, "alice@gmail.com")

    async def test_register_send_code_uses_pre_registration_verification_id(self):
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
            ]
        )
        payload = auth_module.AuthRegisterSendCodeRequest(email="alice@qq.com")
        background_tasks = _BackgroundTasks()

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)):
            result = await auth_module.register_send_code(payload, _request(ip="8.8.8.8"), background_tasks, db)

        self.assertTrue(result.verification_id.startswith("regv_"))
        verification = next(obj for obj in db.added if obj.__class__.__name__ == "EmailVerificationCode")
        self.assertEqual(verification.user_id, result.verification_id)
        self.assertNotEqual(verification.user_id, "u_1")

    async def test_register_send_code_accepts_enterprise_email_domain(self):
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
            ]
        )
        payload = auth_module.AuthRegisterSendCodeRequest(email="jiangtao.sheng@corp.example.ai")
        background_tasks = _BackgroundTasks()

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)):
            result = await auth_module.register_send_code(payload, _request(), background_tasks, db)

        self.assertEqual(result.email, "jiangtao.sheng@corp.example.ai")
        self.assertTrue(result.verification_id.startswith("regv_"))

    async def test_register_check_code_marks_verification_consumed(self):
        now = datetime.utcnow()
        verification = SimpleNamespace(
            user_id="regv_123",
            email="alice@gmail.com",
            purpose="register",
            consumed_at=None,
            attempts=0,
            expires_at=now + timedelta(minutes=10),
            code_hash=auth_module._hash_email_code("123456"),
        )
        db = _FakeDB(execute_results=[_ScalarOneOrNoneResult(verification)])
        payload = auth_module.AuthRegisterCheckCodeRequest(verification_id="regv_123", code="123456")

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)):
            result = await auth_module.register_check_code(payload, _request(), db)

        self.assertTrue(result.verified)
        self.assertEqual(result.email, "alice@gmail.com")
        self.assertIsNotNone(verification.consumed_at)
        self.assertEqual(db.commits, 1)

    async def test_register_with_code_verifies_email_and_returns_session_key(self):
        now = datetime.utcnow()
        verification = SimpleNamespace(
            user_id="regv_123",
            email="alice@gmail.com",
            purpose="register",
            consumed_at=None,
            attempts=0,
            code_hash=auth_module._hash_email_code("123456"),
            expires_at=now + timedelta(minutes=10),
        )
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(verification),  # verification lookup
                _ScalarOneOrNoneResult(None),  # existing account
                _ScalarOneOrNoneResult(None),  # email owner
                _ScalarOneOrNoneResult(None),  # user by username
            ]
        )
        payload = auth_module.AuthRegisterRequest(
            username="alice",
            email="Alice@Gmail.com",
            password="secret123",
            referral_code=None,
            verification_id="regv_123",
            verification_code="123456",
        )

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)), patch.object(
            auth_module, "ensure_finance_summary_initialized", AsyncMock()
        ), patch.object(
            auth_module, "generate_api_key", return_value="sk_cc_session_test"
        ), patch.object(auth_module, "hash_key", return_value="hashed-session"), patch.object(
            auth_module, "encrypt_api_key", return_value="encrypted-session"
        ):
            result = await auth_module.register(payload, _request(), db)

        self.assertEqual(result.status, "active")
        self.assertEqual(result.email, "alice@gmail.com")
        self.assertEqual(result.session_key, "sk_cc_session_test")
        self.assertEqual(db.flushes, 1)
        self.assertEqual(db.commits, 1)
        self.assertIsNotNone(verification.consumed_at)
        account = next(obj for obj in db.added if obj.__class__.__name__ == "Account")
        self.assertEqual(account.status, "active")
        self.assertTrue(any(getattr(obj, "kind", "") == "session" for obj in db.added))

    async def test_register_with_code_accepts_dot_in_username(self):
        now = datetime.utcnow()
        verification = SimpleNamespace(
            user_id="regv_123",
            email="alice@gmail.com",
            purpose="register",
            consumed_at=None,
            attempts=0,
            code_hash=auth_module._hash_email_code("123456"),
            expires_at=now + timedelta(minutes=10),
        )
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(verification),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
            ]
        )
        payload = auth_module.AuthRegisterRequest(
            username="jiangtao.sheng",
            email="Alice@Gmail.com",
            password="secret123",
            referral_code=None,
            verification_id="regv_123",
            verification_code="123456",
        )

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)), patch.object(
            auth_module, "ensure_finance_summary_initialized", AsyncMock()
        ), patch.object(
            auth_module, "generate_api_key", return_value="sk_cc_session_test"
        ), patch.object(auth_module, "hash_key", return_value="hashed-session"), patch.object(
            auth_module, "encrypt_api_key", return_value="encrypted-session"
        ):
            result = await auth_module.register(payload, _request(), db)

        self.assertEqual(result.username, "jiangtao.sheng")
        account = next(obj for obj in db.added if obj.__class__.__name__ == "Account")
        self.assertEqual(account.username, "jiangtao.sheng")

    async def test_register_without_code_rejected(self):
        verification = SimpleNamespace(
            user_id="regv_123",
            email="alice@gmail.com",
            purpose="register",
            consumed_at=None,
            expires_at=datetime.utcnow() + timedelta(minutes=10),
        )
        db = _FakeDB(execute_results=[_ScalarOneOrNoneResult(verification)])
        payload = auth_module.AuthRegisterRequest(
            username="alice",
            email="alice@gmail.com",
            password="secret123",
            referral_code=None,
            verification_id="regv_123",
            verification_code=None,
        )

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)):
            with self.assertRaises(HTTPException) as ctx:
                await auth_module.register(payload, _request(), db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "请输入验证码")

    async def test_verify_email_activates_account_and_issues_session(self):
        now = datetime.utcnow()
        user = SimpleNamespace(id="u_1", username="alice", email="alice@gmail.com", email_verified_at=None)
        account = SimpleNamespace(
            username="alice",
            status="pending_email",
            failed_attempts=2,
            locked_until=now + timedelta(minutes=5),
            last_login_at=None,
        )
        code = "123456"
        verification = SimpleNamespace(
            user_id="u_1",
            purpose="register",
            consumed_at=None,
            attempts=0,
            expires_at=now + timedelta(minutes=10),
            code_hash=auth_module._hash_email_code(code),
        )
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(user),
                _ScalarOneOrNoneResult(account),
                _ScalarOneOrNoneResult(verification),
            ]
        )
        payload = auth_module.AuthVerifyEmailRequest(user_id="u_1", code=code)

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)), patch.object(
            auth_module, "generate_api_key", return_value="sk_cc_session_test"
        ), patch.object(auth_module, "hash_key", return_value="hashed-session"), patch.object(
            auth_module, "encrypt_api_key", return_value="encrypted-session"
        ):
            result = await auth_module.verify_email(payload, _request(), db)

        self.assertEqual(result.session_key, "sk_cc_session_test")
        self.assertEqual(account.status, "active")
        self.assertEqual(account.failed_attempts, 0)
        self.assertIsNone(account.locked_until)
        self.assertIsNotNone(user.email_verified_at)
        self.assertIsNotNone(verification.consumed_at)
        self.assertTrue(any(getattr(obj, "kind", "") == "session" for obj in db.added))

    async def test_unverified_new_account_cannot_login(self):
        account = SimpleNamespace(
            username="alice",
            linked_user_id="u_1",
            password_hash="stored",
            failed_attempts=0,
            locked_until=None,
            status="pending_email",
        )
        user = SimpleNamespace(id="u_1", email="alice@gmail.com", email_verified_at=None)
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(account),
                _ScalarOneOrNoneResult(user),
            ]
        )
        payload = auth_module.AuthLoginRequest(username="alice", password="secret123")

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)), patch.object(
            auth_module, "verify_password", AsyncMock(return_value=True)
        ):
            with self.assertRaises(HTTPException) as ctx:
                await auth_module.login(payload, _request(), db)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "请先验证邮箱")
        self.assertEqual(db.commits, 0)

    async def test_legacy_account_without_email_can_login(self):
        account = SimpleNamespace(
            username="legacy",
            linked_user_id="u_legacy",
            password_hash="stored",
            failed_attempts=1,
            locked_until=None,
            last_login_at=None,
            status="active",
        )
        user = SimpleNamespace(id="u_legacy", email=None, email_verified_at=None)
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(account),
                _ScalarOneOrNoneResult(user),
            ]
        )
        payload = auth_module.AuthLoginRequest(username="legacy", password="secret123")

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)), patch.object(
            auth_module, "verify_password", AsyncMock(return_value=True)
        ), patch.object(auth_module, "generate_api_key", return_value="sk_cc_legacy_session"), patch.object(
            auth_module, "hash_key", return_value="hashed-legacy-session"
        ), patch.object(auth_module, "encrypt_api_key", return_value="encrypted-legacy-session"):
            result = await auth_module.login(payload, _request(), db)

        self.assertEqual(result.user_id, "u_legacy")
        self.assertEqual(result.session_key, "sk_cc_legacy_session")
        self.assertEqual(account.failed_attempts, 0)
        self.assertEqual(db.commits, 1)

    async def test_legacy_account_with_unverified_email_can_still_login(self):
        account = SimpleNamespace(
            username="legacy",
            linked_user_id="u_legacy",
            password_hash="stored",
            failed_attempts=0,
            locked_until=None,
            last_login_at=None,
            status="active",
        )
        user = SimpleNamespace(id="u_legacy", email="legacy@gmail.com", email_verified_at=None)
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(account),
                _ScalarOneOrNoneResult(user),
            ]
        )
        payload = auth_module.AuthLoginRequest(username="legacy", password="secret123")

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)), patch.object(
            auth_module, "verify_password", AsyncMock(return_value=True)
        ), patch.object(auth_module, "generate_api_key", return_value="sk_cc_legacy_session"), patch.object(
            auth_module, "hash_key", return_value="hashed-legacy-session"
        ), patch.object(auth_module, "encrypt_api_key", return_value="encrypted-legacy-session"):
            result = await auth_module.login(payload, _request(), db)

        self.assertEqual(result.user_id, "u_legacy")
        self.assertEqual(result.session_key, "sk_cc_legacy_session")

    async def test_send_current_user_email_code_updates_email_without_blocking_account(self):
        user = SimpleNamespace(id="u_1", username="alice", email=None, email_verified_at=None, status="active")
        session_key = SimpleNamespace(
            user_id="u_1",
            kind="session",
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(session_key),
                _ScalarOneOrNoneResult(user),
                _ScalarOneOrNoneResult(None),  # email owner
                _ScalarOneOrNoneResult(None),  # latest code for cooldown
            ]
        )
        payload = auth_module.AuthSendEmailCodeRequest(email="Alice@Gmail.com")
        background_tasks = _BackgroundTasks()

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)), patch.object(
            auth_module, "hash_key", return_value="hashed-session"
        ):
            result = await auth_module.send_current_user_email_code(payload, _session_request(), background_tasks, db)

        self.assertEqual(result.email, "alice@gmail.com")
        self.assertTrue(result.email_verification_required)
        self.assertEqual(user.email, "alice@gmail.com")
        self.assertIsNone(user.email_verified_at)
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(background_tasks.tasks), 1)

    async def test_verify_current_user_email_sets_verified_at(self):
        now = datetime.utcnow()
        user = SimpleNamespace(id="u_1", username="alice", email="alice@gmail.com", email_verified_at=None, status="active")
        session_key = SimpleNamespace(
            user_id="u_1",
            kind="session",
            expires_at=now + timedelta(days=1),
        )
        code = "123456"
        verification = SimpleNamespace(
            user_id="u_1",
            email="alice@gmail.com",
            purpose="register",
            consumed_at=None,
            attempts=0,
            expires_at=now + timedelta(minutes=10),
            code_hash=auth_module._hash_email_code(code),
        )
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(session_key),
                _ScalarOneOrNoneResult(user),
                _ScalarOneOrNoneResult(verification),
            ]
        )
        payload = auth_module.AuthVerifyCurrentEmailRequest(code=code)

        with patch.object(auth_module.rate_limiter, "allow", AsyncMock(return_value=True)), patch.object(
            auth_module, "hash_key", return_value="hashed-session"
        ):
            result = await auth_module.verify_current_user_email(payload, _session_request(), db)

        self.assertFalse(result.email_verification_required)
        self.assertIsNotNone(user.email_verified_at)
        self.assertIsNotNone(verification.consumed_at)
        self.assertEqual(db.commits, 1)

    async def test_change_current_user_password_updates_account_password(self):
        now = datetime.utcnow()
        user = SimpleNamespace(id="u_1", username="alice", status="active")
        session_key = SimpleNamespace(
            user_id="u_1",
            kind="session",
            expires_at=now + timedelta(days=1),
        )
        account = SimpleNamespace(
            linked_user_id="u_1",
            password_hash="old-hash",
            failed_attempts=3,
            locked_until=now + timedelta(minutes=5),
        )
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(session_key),
                _ScalarOneOrNoneResult(user),
                _ScalarOneOrNoneResult(account),
            ]
        )
        payload = auth_module.AuthChangePasswordRequest(
            current_password="old-secret",
            new_password="new-secret",
        )

        with patch.object(auth_module, "hash_key", return_value="hashed-session"), patch.object(
            auth_module, "verify_password", AsyncMock(return_value=True)
        ), patch.object(auth_module, "hash_password", AsyncMock(return_value="new-hash")):
            result = await auth_module.change_current_user_password(payload, _session_request(), db)

        self.assertEqual(result.status, "password_updated")
        self.assertEqual(account.password_hash, "new-hash")
        self.assertEqual(account.failed_attempts, 0)
        self.assertIsNone(account.locked_until)
        self.assertEqual(db.commits, 1)

    async def test_change_current_user_password_rejects_wrong_current_password(self):
        now = datetime.utcnow()
        user = SimpleNamespace(id="u_1", username="alice", status="active")
        session_key = SimpleNamespace(
            user_id="u_1",
            kind="session",
            expires_at=now + timedelta(days=1),
        )
        account = SimpleNamespace(linked_user_id="u_1", password_hash="old-hash")
        db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(session_key),
                _ScalarOneOrNoneResult(user),
                _ScalarOneOrNoneResult(account),
            ]
        )
        payload = auth_module.AuthChangePasswordRequest(
            current_password="wrong-secret",
            new_password="new-secret",
        )

        with patch.object(auth_module, "hash_key", return_value="hashed-session"), patch.object(
            auth_module, "verify_password", AsyncMock(return_value=False)
        ):
            with self.assertRaises(HTTPException) as ctx:
                await auth_module.change_current_user_password(payload, _session_request(), db)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(account.password_hash, "old-hash")
        self.assertEqual(db.commits, 0)


if __name__ == "__main__":
    unittest.main()
