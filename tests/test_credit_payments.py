import inspect
import unittest
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

import app.main as main_module
import app.admin as admin_module
import app.payment as payment_module
import app.webhook as webhook_module
from app.billing import CREDIT_CATALOGS, CREDIT_PRODUCTS_BY_ID, PaymentProduct
from app.models import (
    CreditBalance,
    PaymentOrder,
    StationCommissionLedgerEntry,
    TrafficPackBalance,
    UserSubscription,
)
from app.payment_common import PaymentConfirmError, confirm_paid_order, quote_payment_cents
from app.schemas import OrderCreateRequest


class _EntityResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _NestedTransaction(AbstractAsyncContextManager):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDB:
    def __init__(self, execute_results=None):
        self.execute_results = list(execute_results or [])
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    async def execute(self, _query):
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return self.execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    def begin_nested(self):
        return _NestedTransaction()

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("coincoin.test", 443),
            "path": "/v1/orders/create",
            "headers": [],
        }
    )


def _credit_order(**overrides):
    values = {
        "id": "po_1",
        "order_no": "CC_credit_1",
        "user_id": "u_1",
        "amount_rmb": "59.90",
        "status": "pending",
        "add_balance_cents": 10000,
        "product_id": "credit_light",
        "catalog_version": "credit-v1",
        "purchase_action": "credit_purchase",
        "promised_credit_cents": 12345,
        "station_id": None,
        "trade_no": None,
        "created_at": None,
        "confirmed_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class CreditPaymentTests(unittest.IsolatedAsyncioTestCase):
    def test_order_history_serializes_frozen_credit_commitment(self):
        payload = payment_module._payment_order_payload(_credit_order())

        self.assertEqual(payload["product_id"], "credit_light")
        self.assertEqual(payload["catalog_version"], "credit-v1")
        self.assertEqual(payload["purchase_action"], "credit_purchase")
        self.assertEqual(payload["promised_credit_cents"], 12345)

    def test_credit_quotes_require_exact_catalog_rmb_amounts(self):
        try:
            quoted = {
                product_id: quote_payment_cents(money, product_id)
                for product_id, money in (
                    ("credit_light", "59.90"),
                    ("credit_standard", "199.00"),
                    ("credit_pro", "399.00"),
                )
            }
        except PaymentConfirmError as exc:
            self.fail(f"public credit quote was rejected: {exc.detail}")

        self.assertEqual(
            quoted,
            {"credit_light": 10000, "credit_standard": 40000, "credit_pro": 100000},
        )
        with self.assertRaisesRegex(PaymentConfirmError, "amount does not match"):
            quote_payment_cents("59.91", "credit_light")
        with self.assertRaises(PaymentConfirmError):
            quote_payment_cents("59.901", "credit_light")
        with self.assertRaisesRegex(PaymentConfirmError, "unknown payment product"):
            quote_payment_cents("49.90", "monthly_light")
        with self.assertRaisesRegex(PaymentConfirmError, "unknown payment product"):
            quote_payment_cents("149.00", "addon_boost")

    async def test_create_order_freezes_credit_catalog_commitment(self):
        db = _FakeDB()
        user = SimpleNamespace(id="u_1")
        payload = OrderCreateRequest(
            name="标准美金额度 $400",
            money="199.00",
            pay_type="alipay",
            product_id="credit_standard",
        )
        with (
            patch.object(payment_module, "epay_configured", return_value=True),
            patch.object(payment_module.rate_limiter, "allow", AsyncMock(return_value=True)),
            patch.object(payment_module, "authenticate_user", AsyncMock(return_value=user)),
            patch.object(payment_module, "build_epay_submit_url", return_value="https://pay.test/order"),
            patch.object(payment_module, "attach_station_to_order", AsyncMock(return_value=None)),
        ):
            try:
                response = await payment_module.create_order(payload, _request(), db)
            except HTTPException as exc:
                self.fail(f"credit order creation was rejected: {exc.detail}")

        self.assertEqual(response.amount_rmb, "199.00")
        self.assertEqual(response.expected_cents, 40000)
        order = next(item for item in db.added if isinstance(item, PaymentOrder))
        self.assertEqual(order.product_id, "credit_standard")
        self.assertEqual(getattr(order, "catalog_version", None), "credit-v1")
        self.assertEqual(getattr(order, "purchase_action", None), "credit_purchase")
        self.assertEqual(getattr(order, "promised_credit_cents", None), 40000)
        self.assertEqual(db.commits, 1)

    async def test_create_order_rejects_old_products_and_inexact_credit_amount(self):
        user = SimpleNamespace(id="u_1")
        for product_id, money, expected_detail in (
            ("monthly_basic", "199.00", "unknown payment product"),
            ("addon_boost", "149.00", "unknown payment product"),
            ("credit_light", "59.91", "amount does not match selected product"),
            ("credit_light", "59.901", "invalid money amount"),
        ):
            with (
                self.subTest(product_id=product_id, money=money),
                patch.object(payment_module, "epay_configured", return_value=True),
                patch.object(payment_module.rate_limiter, "allow", AsyncMock(return_value=True)),
                patch.object(payment_module, "authenticate_user", AsyncMock(return_value=user)),
                patch.object(payment_module, "build_epay_submit_url", return_value="https://pay.test/order"),
                patch.object(payment_module, "attach_station_to_order", AsyncMock(return_value=None)),
            ):
                with self.assertRaises(HTTPException) as caught:
                    await payment_module.create_order(
                        OrderCreateRequest(money=money, product_id=product_id),
                        _request(),
                        _FakeDB(execute_results=[_EntityResult(None)]),
                    )
                self.assertEqual(caught.exception.status_code, 400)
                self.assertIn(expected_detail, str(caught.exception.detail))

    def test_payment_order_schema_and_startup_ddl_include_frozen_commitment(self):
        columns = set(PaymentOrder.__table__.columns.keys())
        expected = {"catalog_version", "purchase_action", "promised_credit_cents"}
        self.assertTrue(expected.issubset(columns), expected - columns)
        migration_source = inspect.getsource(main_module._run_migrations)
        for column in expected:
            self.assertIn(f'"{column}"', migration_source)

    async def test_confirm_grants_one_frozen_credit_batch_and_replay_is_idempotent(self):
        order = _credit_order(
            station_id="st_1",
            station_commission_rate=0.2,
            station_commission_rmb_cents=0,
            station_payout_status="pending",
        )
        user = SimpleNamespace(id="u_1", balance=777, referred_by=None, status="active")
        first_db = _FakeDB(
            execute_results=[
                _EntityResult(order),
                _EntityResult(user),
                _EntityResult(None),
                _EntityResult(None),
                _EntityResult(None),
            ]
        )

        with (
            patch("app.payment_common.ensure_finance_summary_initialized", AsyncMock(return_value=None)),
            patch("app.payment_common.increment_finance_summary", AsyncMock(return_value=None)) as finance_increment,
            patch("app.payment_common.process_referral_reward", AsyncMock(return_value=None)) as referral_reward,
        ):
            first = await confirm_paid_order(
                order_no=order.order_no,
                money="59.90",
                trade_no="trade_1",
                db=first_db,
            )
            station_entries = [
                item for item in first_db.added if isinstance(item, StationCommissionLedgerEntry)
            ]
            self.assertEqual(len(station_entries), 1)
            station_entry = station_entries[0]
            replay_db = _FakeDB(
                execute_results=[
                    _EntityResult(order),
                    _EntityResult(user),
                    _EntityResult(station_entry),
                ]
            )
            replay = await confirm_paid_order(
                order_no=order.order_no,
                money="59.90",
                trade_no="trade_1",
                db=replay_db,
            )

        batches = [item for item in first_db.added if isinstance(item, CreditBalance)]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].source_type, "payment_order")
        self.assertEqual(batches[0].source_id, order.order_no)
        self.assertEqual(batches[0].product_id, "credit_light")
        self.assertEqual(batches[0].original_cents, 12345)
        self.assertEqual(batches[0].remaining_cents, 12345)
        self.assertEqual(
            len([item for item in first_db.added if isinstance(item, StationCommissionLedgerEntry)]),
            1,
        )
        self.assertFalse(
            any(isinstance(item, StationCommissionLedgerEntry) for item in replay_db.added)
        )
        self.assertEqual(first["billing_action"], "credit_purchase")
        self.assertEqual(first["added_cents"], 12345)
        self.assertTrue(replay["already_confirmed"])
        self.assertEqual(user.balance, 777)
        self.assertFalse(any(isinstance(item, UserSubscription) for item in first_db.added))
        self.assertFalse(any(isinstance(item, TrafficPackBalance) for item in first_db.added))
        self.assertEqual(finance_increment.await_count, 1)
        self.assertEqual(referral_reward.await_count, 1)
        self.assertEqual(first_db.commits, 1)
        self.assertEqual(replay_db.commits, 0)

    async def test_every_version_catalog_member_passes_frozen_membership_validation(self):
        catalog_products = dict(CREDIT_PRODUCTS_BY_ID)
        catalog_products["credit_catalog_probe"] = PaymentProduct(
            "credit_catalog_probe",
            "credit",
            "测试美金额度 $777",
            "1.00",
            1,
        )
        with patch.dict(CREDIT_PRODUCTS_BY_ID, catalog_products, clear=True):
            for product_id in catalog_products:
                with self.subTest(product_id=product_id):
                    order = _credit_order(
                        product_id=product_id,
                        promised_credit_cents=77777,
                    )
                    user = SimpleNamespace(
                        id="u_1",
                        balance=500,
                        referred_by=None,
                        status="active",
                    )
                    db = _FakeDB(execute_results=[_EntityResult(order), _EntityResult(user)])
                    with (
                        patch(
                            "app.payment_common.grant_permanent_credit",
                            AsyncMock(return_value=SimpleNamespace()),
                        ),
                        patch(
                            "app.payment_common.create_station_commission_entry_for_confirmed_order",
                            AsyncMock(return_value=SimpleNamespace(entry=None, created=False)),
                        ),
                        patch("app.payment_common.ensure_finance_summary_initialized", AsyncMock()),
                        patch("app.payment_common.increment_finance_summary", AsyncMock()),
                        patch("app.payment_common.process_referral_reward", AsyncMock()),
                    ):
                        result = await confirm_paid_order(
                            order_no=order.order_no,
                            money=order.amount_rmb,
                            trade_no="",
                            db=db,
                        )

                    self.assertEqual(result["added_cents"], 77777)
                    self.assertEqual(result["order"].product_id, product_id)

    async def test_registered_historical_catalog_survives_current_version_rollover(self):
        next_product = PaymentProduct(
            "credit_next",
            "credit",
            "下一版美金额度 $200",
            "99.00",
            20000,
        )
        order = _credit_order(
            catalog_version="credit-v1",
            product_id="credit_light",
            promised_credit_cents=54321,
        )
        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
        db = _FakeDB(execute_results=[_EntityResult(order), _EntityResult(user)])

        with (
            patch.dict(CREDIT_CATALOGS, {"credit-v2": {next_product.id: next_product}}),
            patch.object(payment_module, "CREDIT_CATALOG_VERSION", "credit-v2"),
            patch("app.payment_common.CREDIT_CATALOG_VERSION", "credit-v2", create=True),
            patch(
                "app.payment_common.grant_permanent_credit",
                AsyncMock(return_value=SimpleNamespace()),
            ) as grant,
            patch(
                "app.payment_common.create_station_commission_entry_for_confirmed_order",
                AsyncMock(return_value=SimpleNamespace(entry=None, created=False)),
            ),
            patch("app.payment_common.ensure_finance_summary_initialized", AsyncMock()),
            patch("app.payment_common.increment_finance_summary", AsyncMock()),
            patch("app.payment_common.process_referral_reward", AsyncMock()),
        ):
            result = await confirm_paid_order(
                order_no=order.order_no,
                money=order.amount_rmb,
                trade_no="",
                db=db,
            )

        self.assertEqual(result["added_cents"], 54321)
        self.assertEqual(grant.await_args.kwargs["amount_cents"], 54321)
        self.assertEqual(order.catalog_version, "credit-v1")

    async def test_unregistered_frozen_catalog_version_is_rejected(self):
        order = _credit_order(catalog_version="credit-retired")
        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
        db = _FakeDB(execute_results=[_EntityResult(order), _EntityResult(user)])

        with self.assertRaisesRegex(PaymentConfirmError, "frozen") as caught:
            await confirm_paid_order(
                order_no=order.order_no,
                money=order.amount_rmb,
                trade_no="",
                db=db,
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(order.status, "pending")
        self.assertEqual(db.commits, 0)

    async def test_confirm_rejects_non_allowlisted_frozen_product_before_side_effects(self):
        for product_id in ("monthly_basic", "addon_boost", "credit_unknown"):
            with self.subTest(product_id=product_id):
                order = _credit_order(product_id=product_id, promised_credit_cents=77777)
                user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
                db = _FakeDB(execute_results=[_EntityResult(order), _EntityResult(user)])
                with (
                    patch("app.payment_common.grant_permanent_credit", AsyncMock()) as grant,
                    patch(
                        "app.payment_common.create_station_commission_entry_for_confirmed_order",
                        AsyncMock(),
                        create=True,
                    ) as station_commission,
                    patch("app.payment_common.ensure_finance_summary_initialized", AsyncMock()) as finance_init,
                    patch("app.payment_common.increment_finance_summary", AsyncMock()) as finance_increment,
                    patch("app.payment_common.process_referral_reward", AsyncMock()) as referral_reward,
                ):
                    with self.assertRaisesRegex(PaymentConfirmError, "frozen") as caught:
                        await confirm_paid_order(
                            order_no=order.order_no,
                            money=order.amount_rmb,
                            trade_no="",
                            db=db,
                        )

                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(order.status, "pending")
                self.assertIsNone(order.trade_no)
                grant.assert_not_awaited()
                station_commission.assert_not_awaited()
                finance_init.assert_not_awaited()
                finance_increment.assert_not_awaited()
                referral_reward.assert_not_awaited()
                self.assertEqual(db.commits, 0)

    async def test_webhook_first_confirmation_and_replay_share_station_owner(self):
        order = _credit_order(promised_credit_cents=10000)
        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
        db = _FakeDB(
            execute_results=[
                _EntityResult(order),
                _EntityResult(user),
                _EntityResult(None),
                _EntityResult(order),
                _EntityResult(user),
            ]
        )
        callback = {
            "out_trade_no": order.order_no,
            "money": order.amount_rmb,
            "trade_no": "trade_webhook",
        }
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("coincoin.test", 443),
                "path": "/webhook/pay-notify",
                "query_string": b"",
                "headers": [],
            }
        )

        with (
            patch.object(webhook_module, "verify_epay_callback_params", return_value=callback),
            patch("app.payment_common.grant_permanent_credit", AsyncMock(return_value=SimpleNamespace())),
            patch(
                "app.payment_common.create_station_commission_entry_for_confirmed_order",
                AsyncMock(return_value=SimpleNamespace(entry=None, created=False)),
                create=True,
            ) as station_commission,
            patch("app.payment_common.ensure_finance_summary_initialized", AsyncMock()),
            patch("app.payment_common.increment_finance_summary", AsyncMock()),
            patch("app.payment_common.process_referral_reward", AsyncMock()),
        ):
            first = await webhook_module._handle_pay_notify(request, db)
            replay = await webhook_module._handle_pay_notify(request, db)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(station_commission.await_count, 2)
        self.assertEqual(db.commits, 1)

    async def test_admin_manual_confirmation_uses_common_station_owner(self):
        order = _credit_order(promised_credit_cents=10000)
        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
        db = _FakeDB(
            execute_results=[
                _EntityResult(order),
                _EntityResult(order),
                _EntityResult(user),
                _EntityResult(None),
            ]
        )
        callback = {
            "out_trade_no": order.order_no,
            "money": order.amount_rmb,
            "trade_no": "trade_admin",
        }

        with (
            patch.object(admin_module, "extract_epay_params_from_proof_url", return_value={}),
            patch.object(admin_module, "verify_epay_callback_params", return_value=callback),
            patch("app.payment_common.grant_permanent_credit", AsyncMock(return_value=SimpleNamespace())),
            patch(
                "app.payment_common.create_station_commission_entry_for_confirmed_order",
                AsyncMock(return_value=SimpleNamespace(entry=None, created=False)),
                create=True,
            ) as station_commission,
            patch("app.payment_common.ensure_finance_summary_initialized", AsyncMock()),
            patch("app.payment_common.increment_finance_summary", AsyncMock()),
            patch("app.payment_common.process_referral_reward", AsyncMock()),
        ):
            result = await admin_module.manual_confirm_order(
                order.order_no,
                SimpleNamespace(proof_url="https://pay.test/proof"),
                db,
            )

        self.assertEqual(result["status"], "confirmed")
        station_commission.assert_awaited_once_with(db, order)
        self.assertEqual(db.commits, 1)

    async def test_admin_confirmed_replays_backfill_station_commission_once(self):
        for route_name in ("force", "manual"):
            with self.subTest(route_name=route_name):
                order = _credit_order(
                    status="confirmed",
                    trade_no="trade_existing",
                    promised_credit_cents=10000,
                    station_id="st_1",
                    station_commission_rate=0.2,
                    station_commission_rmb_cents=0,
                    station_payout_status="pending",
                )
                user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
                first_db = _FakeDB(
                    execute_results=[
                        _EntityResult(order),
                        _EntityResult(order),
                        _EntityResult(user),
                        _EntityResult(None),
                    ]
                )

                if route_name == "force":
                    first = await admin_module.force_confirm_order(order.order_no, first_db)
                else:
                    first = await admin_module.manual_confirm_order(
                        order.order_no,
                        SimpleNamespace(proof_url="unused-for-confirmed-order"),
                        first_db,
                    )

                station_entries = [
                    item for item in first_db.added if isinstance(item, StationCommissionLedgerEntry)
                ]
                self.assertEqual(len(station_entries), 1)
                station_entry = station_entries[0]

                replay_db = _FakeDB(
                    execute_results=[
                        _EntityResult(order),
                        _EntityResult(order),
                        _EntityResult(user),
                        _EntityResult(station_entry),
                    ]
                )
                if route_name == "force":
                    replay = await admin_module.force_confirm_order(order.order_no, replay_db)
                else:
                    replay = await admin_module.manual_confirm_order(
                        order.order_no,
                        SimpleNamespace(proof_url="unused-for-confirmed-order"),
                        replay_db,
                    )

                self.assertEqual(first["status"], "already_confirmed")
                self.assertEqual(replay["status"], "already_confirmed")
                self.assertFalse(
                    any(isinstance(item, StationCommissionLedgerEntry) for item in replay_db.added)
                )
                self.assertEqual(first_db.commits, 1)
                self.assertEqual(replay_db.commits, 0)

    async def test_confirm_rejects_legacy_pending_order_without_frozen_commitment(self):
        order = _credit_order(
            product_id="monthly_basic",
            amount_rmb="199.00",
            add_balance_cents=40000,
            catalog_version=None,
            purchase_action=None,
            promised_credit_cents=None,
        )
        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
        db = _FakeDB(execute_results=[_EntityResult(order), _EntityResult(user), _EntityResult(None)])

        with (
            patch("app.payment_common.ensure_finance_summary_initialized", AsyncMock(return_value=None)),
            patch("app.payment_common.increment_finance_summary", AsyncMock(return_value=None)),
            patch("app.payment_common.process_referral_reward", AsyncMock(return_value=None)),
        ):
            with self.assertRaisesRegex(PaymentConfirmError, "frozen") as caught:
                await confirm_paid_order(
                    order_no=order.order_no,
                    money="199.00",
                    trade_no="",
                    db=db,
                )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(order.status, "pending")
        self.assertEqual(user.balance, 500)
        self.assertEqual(db.commits, 0)

    async def test_confirm_rejects_callback_amount_beyond_rmb_cent_precision(self):
        order = _credit_order(promised_credit_cents=10000)
        user = SimpleNamespace(id="u_1", balance=500, referred_by=None, status="active")
        db = _FakeDB(
            execute_results=[
                _EntityResult(order),
                _EntityResult(user),
                _EntityResult(None),
            ]
        )

        with (
            patch("app.payment_common.ensure_finance_summary_initialized", AsyncMock(return_value=None)),
            patch("app.payment_common.increment_finance_summary", AsyncMock(return_value=None)),
            patch("app.payment_common.process_referral_reward", AsyncMock(return_value=None)),
        ):
            with self.assertRaises(PaymentConfirmError):
                await confirm_paid_order(
                    order_no=order.order_no,
                    money="59.901",
                    trade_no="",
                    db=db,
                )

        self.assertEqual(order.status, "pending")
        self.assertEqual(user.balance, 500)
        self.assertEqual(db.commits, 0)


if __name__ == "__main__":
    unittest.main()
