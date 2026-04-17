import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.stations as stations_module
import app.station_settlement as station_settlement_module


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FirstResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _ScalarValueResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FirstResult:
    def __init__(self, value):
        self._value = value

    def first(self):
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


class StationCenterTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_station_customer_creates_user_link_and_api_key(self):
        owner = SimpleNamespace(id="u_owner")
        station = SimpleNamespace(id="st_1", owner_user_id="u_owner", status="active")
        fake_db = _FakeDB(
            execute_results=[
                _FirstResult(None),  # existing linked station user
                _ScalarOneOrNoneResult(None),  # existing user
            ]
        )

        with patch.object(stations_module, "_get_current_user", AsyncMock(return_value=owner)), patch.object(
            stations_module, "_get_owned_station", AsyncMock(return_value=station)
        ):
            payload = stations_module.StationCustomerCreateRequest(username="alice_station_user", create_api_key=True)
            result = await stations_module.create_station_customer(payload, request=None, db=fake_db)

        self.assertTrue(result["success"])
        self.assertEqual(result["station_id"], "st_1")
        self.assertEqual(result["username"], "alice_station_user")
        self.assertTrue(result["api_key"].startswith("sk_cc_"))
        self.assertEqual(fake_db.flushes, 1)
        self.assertEqual(fake_db.commits, 1)
        self.assertEqual(len(fake_db.added), 3)

    async def test_create_station_payout_batch_batches_ready_entries(self):
        original_min = station_settlement_module.settings.station_min_payout_rmb_cents
        station_settlement_module.settings.station_min_payout_rmb_cents = 1000
        try:
            station = SimpleNamespace(
                id="st_1",
                display_name="station one",
                settlement_method="alipay_manual",
                settlement_payee_name="Alice",
                settlement_payee_account="alice@alipay",
                settlement_qr_url="https://cdn.example/alice.png",
            )
            ready_entry = SimpleNamespace(
                id="scl_1",
                status="pending",
                commission_rmb_cents=2500,
                payout_batch_id=None,
            )
            fake_db = _FakeDB(
                execute_results=[
                    _ScalarOneOrNoneResult(station),
                    _ScalarsResult([ready_entry]),
                ]
            )
            request = SimpleNamespace(headers={"authorization": "Bearer admin-token"})
            payload = stations_module.StationPayoutBatchCreateRequest(station_id="st_1", notes="weekly payout")

            result = await stations_module.create_station_payout_batch(payload, request=request, db=fake_db)
        finally:
            station_settlement_module.settings.station_min_payout_rmb_cents = original_min

        self.assertTrue(result["success"])
        self.assertEqual(result["station_id"], "st_1")
        self.assertEqual(result["entry_count"], 1)
        self.assertEqual(result["total_commission_rmb_cents"], 2500)
        self.assertEqual(ready_entry.status, "batched")
        self.assertEqual(fake_db.commits, 1)

    async def test_attach_station_to_order_sets_station_snapshot(self):
        link = SimpleNamespace(station_id="st_1", status="active")
        station = SimpleNamespace(id="st_1", owner_user_id="u_owner", status="active", commission_rate=0.18)
        order = SimpleNamespace(
            station_id=None,
            station_owner_user_id=None,
            station_commission_rate=0.0,
            station_payout_status="none",
        )
        fake_db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(link),
                _ScalarOneOrNoneResult(station),
            ]
        )

        await station_settlement_module.attach_station_to_order(fake_db, order, "u_child")

        self.assertEqual(order.station_id, "st_1")
        self.assertEqual(order.station_owner_user_id, "u_owner")
        self.assertEqual(order.station_commission_rate, 0.18)
        self.assertEqual(order.station_payout_status, "pending")

    async def test_create_station_commission_entry_for_confirmed_order_sets_hold(self):
        original_hold = station_settlement_module.settings.station_payout_hold_days
        station_settlement_module.settings.station_payout_hold_days = 7
        try:
            order = SimpleNamespace(
                id="po_1",
                station_id="st_1",
                user_id="u_1",
                order_no="CC_001",
                amount_rmb="9.90",
                station_commission_rate=0.2,
                station_commission_rmb_cents=0,
                station_payout_status="pending",
                status="confirmed",
            )
            fake_db = _FakeDB(execute_results=[_ScalarOneOrNoneResult(None)])
            entry = await station_settlement_module.create_station_commission_entry_for_confirmed_order(fake_db, order)
        finally:
            station_settlement_module.settings.station_payout_hold_days = original_hold

        self.assertIsNotNone(entry)
        self.assertEqual(entry.station_id, "st_1")
        self.assertEqual(entry.commission_rmb_cents, 198)
        self.assertEqual(order.station_commission_rmb_cents, 198)
        self.assertEqual(order.station_payout_status, "pending")
        self.assertGreater(entry.hold_until, datetime.utcnow() + timedelta(days=6))

    async def test_get_station_summary_aggregates_owner_metrics(self):
        owner = SimpleNamespace(id="u_owner")
        station = SimpleNamespace(
            id="st_1",
            slug="alpha-station",
            display_name="Alpha Station",
            status="active",
            commission_rate=0.15,
            settlement_method="alipay_manual",
            settlement_payee_name="Alice",
            settlement_payee_account="alice@alipay",
            settlement_qr_url="https://example.com/qr.png",
            created_at=datetime.utcnow(),
        )
        payout_paid_at = datetime.utcnow()
        fake_db = _FakeDB(
            execute_results=[
                _ScalarValueResult(3),
                _AllResult([
                    ("pending", 2, 3200),
                    ("batched", 1, 1800),
                    ("paid", 5, 7600),
                ]),
                _AllResult([
                    ("pending", 1, 1800, None),
                    ("paid", 2, 7600, payout_paid_at),
                ]),
            ]
        )

        with patch.object(stations_module, "_get_current_user", AsyncMock(return_value=owner)), patch.object(
            stations_module, "_get_owned_station", AsyncMock(return_value=station)
        ):
            result = await stations_module.get_station_summary(request=None, db=fake_db)

        self.assertEqual(result["station"]["id"], "st_1")
        self.assertEqual(result["customer_count"], 3)
        self.assertEqual(result["commission_summary"]["pending_rmb_cents"], 3200)
        self.assertEqual(result["commission_summary"]["batched_count"], 1)
        self.assertEqual(result["commission_summary"]["paid_rmb_cents"], 7600)
        self.assertEqual(result["payout_summary"]["pending_batch_count"], 1)
        self.assertEqual(result["payout_summary"]["paid_batch_total_rmb_cents"], 7600)
        self.assertIsNotNone(result["payout_summary"]["last_paid_at"])

    async def test_update_station_settlement_persists_owner_config(self):
        owner = SimpleNamespace(id="u_owner")
        station = SimpleNamespace(
            id="st_1",
            slug="alpha-station",
            display_name="Alpha Station",
            status="active",
            commission_rate=0.15,
            settlement_method="alipay_manual",
            settlement_payee_name="Old Name",
            settlement_payee_account="old@alipay",
            settlement_qr_url="",
            created_at=datetime.utcnow(),
        )
        fake_db = _FakeDB()

        with patch.object(stations_module, "_get_current_user", AsyncMock(return_value=owner)), patch.object(
            stations_module, "_get_owned_station", AsyncMock(return_value=station)
        ):
            payload = stations_module.StationSettlementUpdateRequest(
                settlement_method="alipay_manual",
                settlement_payee_name="Alice",
                settlement_payee_account="alice@alipay",
                settlement_qr_url="https://example.com/new.png",
            )
            result = await stations_module.update_station_settlement(payload, request=None, db=fake_db)

        self.assertTrue(result["success"])
        self.assertEqual(result["station"]["settlement_payee_name"], "Alice")
        self.assertEqual(station.settlement_payee_account, "alice@alipay")
        self.assertEqual(fake_db.commits, 1)

    async def test_mark_station_payout_batch_paid_records_proof_fields(self):
        batch = SimpleNamespace(
            id="spb_1",
            status="pending",
            paid_by="",
            paid_at=None,
            payment_reference="",
            payment_screenshot_url="",
            payment_note="",
        )
        ledger_row = SimpleNamespace(status="batched", payout_batch_id="spb_1")
        fake_db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(batch),
                _ScalarsResult([ledger_row]),
            ]
        )
        request = SimpleNamespace(headers={"authorization": "Bearer admin-token"})
        payload = stations_module.StationPayoutBatchMarkPaidRequest(
            payment_reference="ALIPAY-20260417-001",
            payment_screenshot_url="https://example.com/proof.png",
            payment_note="已人工扫码转账",
        )

        result = await stations_module.mark_station_payout_batch_paid(
            "spb_1",
            payload=payload,
            request=request,
            db=fake_db,
        )

        self.assertTrue(result["success"])
        self.assertEqual(batch.status, "paid")
        self.assertEqual(batch.payment_reference, "ALIPAY-20260417-001")
        self.assertEqual(batch.payment_screenshot_url, "https://example.com/proof.png")
        self.assertEqual(batch.payment_note, "已人工扫码转账")
        self.assertEqual(ledger_row.status, "paid")
        self.assertEqual(fake_db.commits, 1)


if __name__ == "__main__":
    unittest.main()
