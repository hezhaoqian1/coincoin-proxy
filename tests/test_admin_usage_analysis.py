"""Exercise the usage API against real SQL aggregates in an isolated SQLite DB.

Only MySQL's date_format/convert_tz functions are emulated. No production DB,
billing writes or FastAPI lifespan jobs are used by these tests.
"""
import csv
import io
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app import admin_usage as usage
from app.config import settings
from app.models import RequestLog, User

NOW = datetime(2026, 9, 17, 4, 0, tzinfo=timezone.utc)
WINDOW = {"period": "custom", "start_date": "2026-09-16", "end_date": "2026-09-16"}


class UsageWindowTests(unittest.TestCase):
    def test_calendar_and_rolling_windows(self):
        expected = {
            'today': ('2026-09-16T16:00:00', '2026-09-17T04:00:00'),
            '24h': ('2026-09-16T04:00:00', '2026-09-17T04:00:00'),
            'yesterday': ('2026-09-15T16:00:00', '2026-09-16T16:00:00'),
            '3d': ('2026-09-14T04:00:00', '2026-09-17T04:00:00'),
            'this_week': ('2026-09-13T16:00:00', '2026-09-17T04:00:00'),
            '7d': ('2026-09-10T04:00:00', '2026-09-17T04:00:00'),
            '30d': ('2026-08-18T04:00:00', '2026-09-17T04:00:00'),
            'this_month': ('2026-08-31T16:00:00', '2026-09-17T04:00:00'),
        }
        for period, bounds in expected.items():
            with self.subTest(period=period):
                self.assertEqual(tuple(v.isoformat() for v in usage.period_bounds(period, now=NOW)), bounds)

    def test_midnight_monday_year_and_leap_boundaries(self):
        monday = datetime(2026, 9, 13, 16, tzinfo=timezone.utc)
        self.assertEqual(usage.period_bounds('this_week', now=monday), (monday.replace(tzinfo=None),) * 2)
        start, end = usage.period_bounds('yesterday', now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual((start, end), (datetime(2025, 12, 30, 16), datetime(2025, 12, 31, 16)))
        self.assertEqual(usage.period_bounds('custom', '2024-02-29', '2024-02-29'),
                         (datetime(2024, 2, 28, 16), datetime(2024, 2, 29, 16)))

    def test_invalid_or_oversized_windows(self):
        for start, end in [('bad', '2026-09-17'), ('2026-09-18', '2026-09-17'), ('2026-01-01', '2026-09-17'), ('9999-12-31', '9999-12-31'), ('2026-09-01', '9999-12-31')]:
            with self.subTest(start=start), self.assertRaises(HTTPException):
                usage.period_bounds('custom', start, end)
        with self.assertRaises(HTTPException):
            usage.period_bounds('never')


class _AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, query):
        return self.session.execute(query)

    async def scalar(self, query):
        return self.session.scalar(query)


class AdminUsageAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine('sqlite://')

        @event.listens_for(self.engine, 'connect')
        def dates(db, _):
            db.create_function('convert_tz', 3, lambda value, source, target:
                               (datetime.fromisoformat(value) + timedelta(hours=8)).isoformat())
            db.create_function('date_format', 2, lambda value, fmt: datetime.fromisoformat(value).strftime(fmt))

        User.__table__.create(self.engine)
        RequestLog.__table__.create(self.engine)
        self.session = Session(self.engine)
        self.session.add_all([User(id='u1', username='alice', email='alice@example.test'),
                              User(id='u2', username="=cmd|' <b>bob</b>", email='bob@example.test')])
        self.session.flush()
        self.add_log('a', 'u1', cost_cents=999, retail_charge_cents=100, input_tokens=1000, output_tokens=200,
                     cached_tokens=800, cache_read_tokens=800, cache_creation_tokens=100, duration_ms=1000,
                     customer_model_alias='public-a', provider_model='upstream-a', billable_sku='sku-a', route_attempt=1)
        self.add_log('b', 'u1', cost_cents=20, input_tokens=100, output_tokens=50, cached_tokens=10,
                     status_code=500, duration_ms=3000, customer_model_alias='public-b')
        self.add_log('c', 'u2', cost_cents=80, video_count=1, customer_model_alias='video', api_key_id='k2')
        self.add_log('d', 'u2', cost_cents=-80, retail_charge_cents=-80, video_count=-1, status_code=500,
                     route_reason='video_job_refund', customer_model_alias='video', api_key_id='k2')
        self.add_log('before', 'u1', created_at=datetime(2026, 9, 15, 15, 59, 59), cost_cents=9000)
        self.add_log('end', 'u1', created_at=datetime(2026, 9, 16, 16), cost_cents=9000)
        self.session.commit()
        self.api = FastAPI()
        self.api.include_router(usage.router, prefix='/admin')
        adapter = _AsyncSessionAdapter(self.session)

        async def get_db():
            yield adapter

        self.api.dependency_overrides[usage.get_db] = get_db
        self.token = patch.object(settings, 'admin_token', 'usage-test-secret')
        self.token.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api), base_url='http://usage.test',
                                       headers={'x-admin-token': 'usage-test-secret'})

    def add_log(self, id, user_id, **fields):
        values = dict(created_at=datetime(2026, 9, 16, 1), model='legacy-model', endpoint='messages:stream',
                      api_key_id='k1', channel_id='channel-a')
        values.update(fields)
        self.session.add(RequestLog(id=id, user_id=user_id, **values))

    async def asyncTearDown(self):
        await self.client.aclose()
        self.token.stop()
        self.session.close()
        self.engine.dispose()

    async def get(self, path, **params):
        response = await self.client.get('/admin/usage/' + path, params={**WINDOW, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_summary_reconciles_refunds_tokens_and_trend(self):
        data = await self.get('overview')
        s = data['summary']
        self.assertEqual((s['cost_cents'], s['refund_cents'], s['videos']), (120, 80, 0))
        self.assertEqual((s['records'], s['requests'], s['succeeded'], s['failed']), (4, 3, 2, 1))
        self.assertEqual((s['tokens'], s['input_tokens'], s['cache_read_tokens'], s['cache_creation_tokens']), (1350, 1100, 810, 100))
        self.assertEqual((s['users'], s['models'], s['fallback_requests'], s['avg_latency_ms']), (2, 3, 1, 2000))
        self.assertAlmostEqual(s['failure_rate'], 1/3)
        self.assertEqual(len(data['trend']), 24)
        self.assertEqual(sum(v['cost_cents'] for v in data['trend']), s['cost_cents'])
        self.assertEqual(sum(v['requests'] for v in data['trend']), s['requests'])
        self.assertEqual(data['trend'][9]['bucket'], '2026-09-16 09:00')

    async def test_groups_pagination_sort_and_user_model_drilldown(self):
        users = await self.get('groups', dimension='users', limit=1)
        self.assertEqual((users['total'], users['data'][0]['key'], users['data'][0]['models']), (2, 'u1', 2))
        second = await self.get('groups', dimension='users', limit=1, offset=1)
        self.assertEqual((second['data'][0]['rank'], second['data'][0]['key']), (2, 'u2'))
        models = await self.get('users/u1/models', result='failed')
        self.assertEqual([v['model'] for v in models['data']], ['public-b'])
        self.assertEqual(models['data'][0]['cost_cents'], 20)
        user_models = await self.get('users/u2/models')
        self.assertEqual([v['model'] for v in user_models['data']], ['video'])
        self.assertEqual(user_models['data'][0]['cost_cents'], 0)
        videos = await self.get('groups', dimension='models', user_id='u2')
        self.assertEqual((videos['total'], videos['data'][0]['cost_cents']), (1, 0))

        model_users = await self.get('groups', dimension='users', model_exact='video')
        self.assertEqual([v['key'] for v in model_users['data']], ['u2'])
        self.assertEqual(model_users['data'][0]['records'], 2)

        model_users = await self.get('groups', dimension='users', model_exact='public-a')
        self.assertEqual([v['key'] for v in model_users['data']], ['u1'])
        self.assertEqual(model_users['data'][0]['cost_cents'], 100)

    async def test_filters_and_export_match_records(self):
        for field, value in [('model', 'upstream-a'), ('model', 'sku-a'), ('model_exact', 'public-a'),
                             ('search', 'alice@example'), ('api_key_id', 'k2'), ('channel_id', 'channel-a'),
                             ('endpoint', 'messages:stream'), ('result', 'refund'), ('result', 'failed')]:
            with self.subTest(field=field, value=value):
                params = {field: value}
                records = await self.get('records', **params)
                summary = await self.get('overview', **params)
                self.assertEqual(records['total'], summary['summary']['records'])
                self.assertEqual(sum(r['cost_cents'] for r in records['data']), summary['summary']['cost_cents'])
                export = await self.client.get('/admin/usage/export.csv', params={**WINDOW, **params})
                self.assertEqual(export.status_code, 200, export.text)
                rows = list(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff'))))
                self.assertEqual(len(rows), records['total'])
                self.assertEqual({r['日志 ID'] for r in rows}, {r['id'] for r in records['data']})

    async def test_literal_search_missing_user_and_empty_results(self):
        for params in [{'model': '%'}, {'search': '%'}, {'user_id': 'unknown'}]:
            data = await self.get('overview', **params)
            self.assertEqual(data['summary']['records'], 0)
            self.assertIsNone(data['summary']['failure_rate'])
        self.assertEqual((await self.get('users/unknown/models'))['data'], [])

    async def test_csv_formula_guard_negative_refund_and_limit(self):
        response = await self.client.get('/admin/usage/export.csv', params=WINDOW)
        self.assertTrue(response.text.startswith('\ufeff'))
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        bob = next(r for r in rows if r['日志 ID'] == 'd')
        self.assertTrue(bob['用户'].startswith("'=cmd"))
        self.assertEqual(bob['日志消耗 美分'], '-80')
        self.assertIn('有效缓存写入价 美分/百万Token', bob)
        with patch.object(usage, 'EXPORT_LIMIT', 2):
            response = await self.client.get('/admin/usage/export.csv', params=WINDOW)
        self.assertEqual(response.status_code, 413)

    async def test_records_stable_pagination_and_detail(self):
        first = await self.get('records', limit=2)
        second = await self.get('records', limit=2, offset=2)
        self.assertEqual([r['id'] for r in first['data'] + second['data']], ['d', 'c', 'b', 'a'])
        a = second['data'][1]
        self.assertEqual((a['tokens'], a['provider_model'], a['cost_cents']), (1200, 'upstream-a', 100))
        self.assertTrue(a['created_at'].endswith('+00:00'))
        self.assertNotIn('encrypted_key', a)

    async def test_optional_pricing_snapshots_do_not_require_a_schema_change(self):
        log = self.session.get(RequestLog, 'a')
        historical = SimpleNamespace(**{column.name: getattr(log, column.name)
                                        for column in RequestLog.__table__.columns
                                        if column.name not in {'user_billing_multiplier',
                                                               'effective_cache_creation_input_per_million'}})
        row = (historical, 'alice', 'alice@example.test', None)
        detail = usage.serialize_record(row)
        self.assertIsNone(detail['user_billing_multiplier'])
        self.assertIsNone(detail['effective_cache_creation_input_per_million'])
        exported = next(csv.DictReader(io.StringIO(usage.render_csv([row]).lstrip('\ufeff'))))
        self.assertEqual(exported['用户倍率'], '')
        self.assertEqual(exported['有效缓存写入价 美分/百万Token'], '')

    async def test_rolling_snapshot_bounds_are_shared(self):
        data = await self.get('overview', period='24h', as_of=NOW.isoformat())
        records = await self.get('records', period='24h', as_of=data['as_of'])
        self.assertEqual(records['window_start'], data['window_start'])
        self.assertEqual(records['window_end'], data['window_end'])

    async def test_optional_totals_avoid_duplicate_count_queries(self):
        queries = []

        def capture(conn, cursor, statement, parameters, context, many):
            queries.append(statement)

        event.listen(self.engine, 'before_cursor_execute', capture)
        try:
            for path in ['groups', 'records']:
                queries.clear()
                data = await self.get(path, include_total='false')
                self.assertIsNone(data['total'])
                self.assertTrue(data['data'])
                self.assertEqual(len(queries), 1)
        finally:
            event.remove(self.engine, 'before_cursor_execute', capture)

    async def test_custom_current_day_stops_at_snapshot(self):
        data = await self.get('overview', start_date='2026-09-17', end_date='2026-09-17', as_of=NOW.isoformat())
        self.assertEqual(data['window_end'], NOW.isoformat())
        self.assertEqual(len(data['trend']), 12)

    async def test_all_routes_require_admin_and_validate_filters(self):
        for path in ['overview', 'groups', 'records', 'users/u1/models', 'export.csv']:
            response = await self.client.get('/admin/usage/' + path, headers={'x-admin-token': 'wrong'})
            self.assertEqual(response.status_code, 401, path)
        for params, status in [({'period': 'invalid'}, 400), ({'period': 'custom'}, 400),
                               ({'as_of': '2026-01-01'}, 400), ({'as_of': '9999-01-01T00:00:00Z'}, 400),
                               ({'result': 'unknown'}, 422), ({'limit': 101}, 422), ({'offset': -1}, 422)]:
            response = await self.client.get('/admin/usage/records', params=params)
            self.assertEqual(response.status_code, status, response.text)
