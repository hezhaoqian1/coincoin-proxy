import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.admin as admin_module
from fastapi import HTTPException
from app.models import ProviderChannel, ProviderChannelMonitor
from app.schemas import AdminProviderChannelMonitorCreate, AdminProviderChannelMonitorUpdate


class _Scalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _Scalars(self._values)


class _MonitorCrudDB:
    def __init__(self, *, channels, monitors=(), scalar_results=(), execute_results=()):
        self.channels = {channel.id: channel for channel in channels}
        self.monitors = {monitor.id: monitor for monitor in monitors}
        self.scalar_results = list(scalar_results)
        self.execute_results = list(execute_results)
        self.queries = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    async def get(self, model, key):
        if model is ProviderChannel:
            return self.channels.get(key)
        if model is ProviderChannelMonitor:
            return self.monitors.get(key)
        return None

    async def scalar(self, query):
        self.queries.append(query)
        if not self.scalar_results:
            raise AssertionError("unexpected scalar call")
        return self.scalar_results.pop(0)

    async def execute(self, query):
        self.queries.append(query)
        if not self.execute_results:
            raise AssertionError("unexpected execute call")
        return self.execute_results.pop(0)

    def add(self, value):
        self.added.append(value)
        if isinstance(value, ProviderChannelMonitor):
            self.monitors[value.id] = value

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _channel(channel_id):
    return SimpleNamespace(
        id=channel_id,
        name=channel_id,
        channel_type="openai_compatible",
        status="active",
        priority=0,
        weight=1,
    )


def _monitor(monitor_id, channel_id, *, status="active", created_by="route-reconciler"):
    return SimpleNamespace(
        id=monitor_id,
        channel_id=channel_id,
        name=monitor_id,
        endpoint="responses",
        primary_model="gpt-test",
        extra_models="[]",
        status=status,
        interval_seconds=300,
        timeout_seconds=30,
        claimed_until=None,
        created_by=created_by,
    )


class ProviderChannelMonitorApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_locks_channel_reconciles_once_and_invalidates_after_commit(self) -> None:
        channel = _channel("ch_create")
        existing_auto = _monitor("cma_create", channel.id)
        db = _MonitorCrudDB(
            channels=[channel],
            monitors=[existing_auto],
            execute_results=[_ScalarsResult([channel])],
        )
        payload = AdminProviderChannelMonitorCreate(
            channel_id=channel.id,
            primary_model="gpt-manual",
            endpoint="responses",
        )

        async def reconcile(candidate_db, *, commit=True):
            self.assertIs(candidate_db, db)
            self.assertFalse(commit)
            self.assertEqual(db.flushes, 1)
            self.assertEqual(db.commits, 0)
            existing_auto.status = "disabled"
            return {"created": 0, "updated": 0, "disabled": 1}

        with (
            patch.object(admin_module, "reconcile_provider_channel_monitors", side_effect=reconcile) as reconcile_mock,
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            response = await admin_module.create_provider_channel_monitor(payload, db)

        body = json.loads(response.body)
        created = db.added[0]
        self.assertEqual(body, {
            "id": created.id,
            "channel_id": channel.id,
            "name": "ch_create",
            "endpoint": "responses",
            "primary_model": "gpt-manual",
            "extra_models": [],
            "status": "active",
            "interval_seconds": 300,
            "timeout_seconds": 30,
        })
        self.assertEqual(created.created_by, "admin-override")
        self.assertEqual([created.status, existing_auto.status].count("active"), 1)
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        self.assertIn("FOR UPDATE", str(db.queries[0]).upper())
        reconcile_mock.assert_awaited_once_with(db, commit=False)
        invalidate.assert_called_once_with()

    async def test_create_rolls_back_and_does_not_invalidate_when_reconcile_fails(self) -> None:
        channel = _channel("ch_create_failure")
        db = _MonitorCrudDB(
            channels=[channel],
            execute_results=[_ScalarsResult([channel])],
        )
        payload = AdminProviderChannelMonitorCreate(
            channel_id=channel.id,
            primary_model="gpt-manual",
        )

        with (
            patch.object(
                admin_module,
                "reconcile_provider_channel_monitors",
                AsyncMock(side_effect=RuntimeError("reconcile failed")),
            ),
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            with self.assertRaisesRegex(RuntimeError, "reconcile failed"):
                await admin_module.create_provider_channel_monitor(payload, db)

        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)
        invalidate.assert_not_called()

    async def test_patch_move_locks_old_and_new_channels_and_reconciles_both(self) -> None:
        old_channel = _channel("ch_old")
        new_channel = _channel("ch_new")
        moved = _monitor("cmon_move", old_channel.id, created_by="admin")
        old_auto = _monitor("cma_old", old_channel.id, status="disabled")
        new_auto = _monitor("cma_new", new_channel.id)
        db = _MonitorCrudDB(
            channels=[old_channel, new_channel],
            monitors=[moved, old_auto, new_auto],
            scalar_results=[moved],
            execute_results=[_ScalarsResult([new_channel, old_channel])],
        )
        payload = AdminProviderChannelMonitorUpdate(channel_id=new_channel.id, primary_model="gpt-moved")

        async def reconcile(candidate_db, *, commit=True):
            self.assertIs(candidate_db, db)
            self.assertFalse(commit)
            old_auto.status = "active"
            new_auto.status = "disabled"
            moved.status = "active"
            return {"created": 0, "updated": 1, "disabled": 1}

        with (
            patch.object(admin_module, "reconcile_provider_channel_monitors", side_effect=reconcile),
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            response = await admin_module.update_provider_channel_monitor(moved.id, payload, db)

        body = json.loads(response.body)
        self.assertEqual(body["id"], moved.id)
        self.assertEqual(body["channel_id"], new_channel.id)
        self.assertEqual(body["primary_model"], "gpt-moved")
        self.assertEqual(moved.created_by, "admin-override")
        self.assertEqual([old_auto.status].count("active"), 1)
        self.assertEqual([moved.status, new_auto.status].count("active"), 1)
        self.assertEqual(db.flushes, 1)
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        self.assertTrue(all("FOR UPDATE" in str(query).upper() for query in db.queries))
        self.assertIn("coincoin_provider_channels", str(db.queries[0]))
        self.assertIn("coincoin_provider_channel_monitors", str(db.queries[1]))
        invalidate.assert_called_once_with()

    async def test_patch_returns_conflict_when_monitor_channel_changes_after_channel_lock(self) -> None:
        old_channel = _channel("ch_patch_old")
        target_channel = _channel("ch_patch_target")
        changed_channel = _channel("ch_patch_changed")
        optimistic = _monitor("cmon_patch_race", old_channel.id, created_by="admin")
        changed = _monitor(optimistic.id, changed_channel.id, created_by="admin")
        db = _MonitorCrudDB(
            channels=[old_channel, target_channel, changed_channel],
            monitors=[optimistic],
            scalar_results=[changed],
            execute_results=[_ScalarsResult([old_channel, target_channel])],
        )
        payload = AdminProviderChannelMonitorUpdate(channel_id=target_channel.id)

        with patch.object(admin_module, "invalidate_reliability_cache") as invalidate:
            with self.assertRaises(HTTPException) as raised:
                await admin_module.update_provider_channel_monitor(optimistic.id, payload, db)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("changed channels", raised.exception.detail)
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)
        invalidate.assert_not_called()

    async def test_delete_locks_channel_then_monitor_reconciles_and_invalidates_after_commit(self) -> None:
        channel = _channel("ch_delete")
        monitor = _monitor("cmon_delete", channel.id, created_by="admin-override")
        replacement = _monitor("cma_delete", channel.id, status="disabled")
        db = _MonitorCrudDB(
            channels=[channel],
            monitors=[monitor, replacement],
            scalar_results=[monitor],
            execute_results=[
                _ScalarsResult([channel]),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(),
            ],
        )

        async def reconcile(candidate_db, *, commit=True):
            self.assertIs(candidate_db, db)
            self.assertFalse(commit)
            self.assertEqual(db.flushes, 1)
            self.assertEqual(db.commits, 0)
            replacement.status = "active"
            return {"created": 0, "updated": 1, "disabled": 0}

        with (
            patch.object(admin_module, "reconcile_provider_channel_monitors", side_effect=reconcile) as reconcile_mock,
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            response = await admin_module.delete_provider_channel_monitor(monitor.id, db)

        self.assertEqual(response, {"deleted": True, "id": monitor.id})
        self.assertEqual(replacement.status, "active")
        self.assertEqual(db.flushes, 1)
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        self.assertIn("coincoin_provider_channels", str(db.queries[0]))
        self.assertIn("coincoin_provider_channel_monitors", str(db.queries[1]))
        reconcile_mock.assert_awaited_once_with(db, commit=False)
        invalidate.assert_called_once_with()

    async def test_delete_rejects_active_claim_without_mutation_or_cache_invalidation(self) -> None:
        channel = _channel("ch_delete_claimed")
        monitor = _monitor("cmon_delete_claimed", channel.id)
        monitor.claimed_until = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=2)
        db = _MonitorCrudDB(
            channels=[channel],
            monitors=[monitor],
            scalar_results=[monitor],
            execute_results=[_ScalarsResult([channel])],
        )

        with (
            patch.object(admin_module, "reconcile_provider_channel_monitors", AsyncMock()) as reconcile,
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            with self.assertRaises(HTTPException) as raised:
                await admin_module.delete_provider_channel_monitor(monitor.id, db)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(len(db.queries), 2)
        reconcile.assert_not_awaited()
        invalidate.assert_not_called()

    async def test_delete_rolls_back_and_does_not_invalidate_when_reconcile_fails(self) -> None:
        channel = _channel("ch_delete_failure")
        monitor = _monitor("cmon_delete_failure", channel.id)
        db = _MonitorCrudDB(
            channels=[channel],
            monitors=[monitor],
            scalar_results=[monitor],
            execute_results=[
                _ScalarsResult([channel]),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(),
            ],
        )

        with (
            patch.object(
                admin_module,
                "reconcile_provider_channel_monitors",
                AsyncMock(side_effect=RuntimeError("delete reconcile failed")),
            ),
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            with self.assertRaisesRegex(RuntimeError, "delete reconcile failed"):
                await admin_module.delete_provider_channel_monitor(monitor.id, db)

        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)
        invalidate.assert_not_called()

    async def test_explicit_run_rejects_current_claim_without_upstream_or_cache_invalidation(self) -> None:
        channel = _channel("ch_claimed")
        monitor = _monitor("cmon_claimed", channel.id)
        monitor.claimed_until = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=2)
        db = _MonitorCrudDB(
            channels=[channel],
            monitors=[monitor],
            scalar_results=[monitor],
        )

        with (
            patch.object(admin_module, "run_provider_channel_monitor_once", AsyncMock()) as run_once,
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            with self.assertRaises(HTTPException) as raised:
                await admin_module.run_provider_channel_monitor_now(monitor.id, db)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)
        run_once.assert_not_awaited()
        invalidate.assert_not_called()

    async def test_explicit_run_commits_lease_before_upstream_and_invalidates_on_success(self) -> None:
        channel = _channel("ch_run")
        monitor = _monitor("cmon_run", channel.id)
        db = _MonitorCrudDB(
            channels=[channel],
            monitors=[monitor],
            scalar_results=[monitor],
        )
        result = SimpleNamespace(
            model="gpt-test",
            status="operational",
            latency_ms=100,
            ping_latency_ms=0,
            status_code=200,
            message="ok",
            checked_at=datetime(2026, 7, 15, 10, 0, 0),
        )

        async def run_once(candidate_db, monitor_id):
            self.assertIs(candidate_db, db)
            self.assertEqual(monitor_id, monitor.id)
            self.assertEqual(db.commits, 1)
            self.assertGreater(monitor.claimed_until, datetime.now(UTC).replace(tzinfo=None))
            monitor.claimed_until = None
            return [result]

        with (
            patch.object(admin_module, "run_provider_channel_monitor_once", side_effect=run_once),
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            response = await admin_module.run_provider_channel_monitor_now(monitor.id, db)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.commits, 1)
        self.assertIsNone(monitor.claimed_until)
        invalidate.assert_called_once_with()

    async def test_explicit_run_failure_keeps_bounded_lease_and_does_not_invalidate(self) -> None:
        channel = _channel("ch_run_failure")
        monitor = _monitor("cmon_run_failure", channel.id)
        db = _MonitorCrudDB(
            channels=[channel],
            monitors=[monitor],
            scalar_results=[monitor],
        )

        async def fail_after_claim(candidate_db, monitor_id):
            self.assertIs(candidate_db, db)
            self.assertEqual(monitor_id, monitor.id)
            self.assertEqual(db.commits, 1)
            raise RuntimeError("upstream exploded")

        with (
            patch.object(admin_module, "run_provider_channel_monitor_once", side_effect=fail_after_claim),
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            with self.assertRaisesRegex(RuntimeError, "upstream exploded"):
                await admin_module.run_provider_channel_monitor_now(monitor.id, db)

        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 1)
        self.assertGreater(monitor.claimed_until, datetime.now(UTC).replace(tzinfo=None))
        invalidate.assert_not_called()

    async def test_explicit_run_times_out_before_committed_lease_expires(self) -> None:
        channel = _channel("ch_run_timeout")
        monitor = _monitor("cmon_run_timeout", channel.id)
        db = _MonitorCrudDB(
            channels=[channel],
            monitors=[monitor],
            scalar_results=[monitor],
        )

        async def force_timeout(awaitable, timeout):
            remaining = (
                monitor.claimed_until - datetime.now(UTC).replace(tzinfo=None)
            ).total_seconds()
            self.assertGreater(remaining, timeout)
            awaitable.close()
            raise TimeoutError("probe exceeded hard timeout")

        with (
            patch.object(admin_module, "run_provider_channel_monitor_once", AsyncMock()) as run_once,
            patch("asyncio.wait_for", side_effect=force_timeout) as wait_for,
            patch.object(admin_module, "invalidate_reliability_cache") as invalidate,
        ):
            with self.assertRaises(HTTPException) as raised:
                await admin_module.run_provider_channel_monitor_now(monitor.id, db)

        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 1)
        self.assertGreater(monitor.claimed_until, datetime.now(UTC).replace(tzinfo=None))
        run_once.assert_called_once_with(db, monitor.id)
        wait_for.assert_awaited_once()
        invalidate.assert_not_called()
