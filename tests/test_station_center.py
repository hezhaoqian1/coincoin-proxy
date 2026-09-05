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
    def _station_url_settings_snapshot(self):
        return {
            "station_public_base_url": stations_module.settings.station_public_base_url,
            "station_portal_domain": stations_module.settings.station_portal_domain,
            "station_api_domain": stations_module.settings.station_api_domain,
            "station_api_base_url": stations_module.settings.station_api_base_url,
            "station_portal_path_prefix": stations_module.settings.station_portal_path_prefix,
            "station_api_path_prefix": stations_module.settings.station_api_path_prefix,
        }

    def _restore_station_url_settings(self, snapshot):
        for name, value in snapshot.items():
            setattr(stations_module.settings, name, value)

    def _station_row_for_url_tests(self):
        return SimpleNamespace(
            id="st_1",
            slug="stone",
            display_name="Stone Station",
            status="active",
            commission_rate=0.15,
            settlement_method="alipay_manual",
            settlement_payee_name="Alice",
            settlement_payee_account="alice@alipay",
            settlement_qr_url="https://example.com/qr.png",
            created_at=datetime.utcnow(),
        )

    async def test_get_public_station_returns_branding_and_active_aliases(self):
        station = SimpleNamespace(
            id="st_1",
            slug="stone",
            display_name="Stone Station",
            status="active",
            mode="commission_station",
            balance_cents=0,
            currency="usd_cents",
            wholesale_tier="standard",
            default_text_alias="fast",
            default_image_alias="image",
            commission_rate=0.15,
            settlement_method="alipay_manual",
            settlement_payee_name="",
            settlement_payee_account="",
            settlement_qr_url="",
            created_at=datetime.utcnow(),
        )
        branding = SimpleNamespace(
            station_id="st_1",
            display_name="Stone AI",
            logo_url="https://cdn.example/logo.png",
            favicon_url="https://cdn.example/favicon.png",
            support_email="support@stone.example",
            support_link="https://stone.example/help",
            docs_intro="Stone docs",
            terms_url="https://stone.example/terms",
            updated_at=datetime.utcnow(),
        )
        alias = SimpleNamespace(
            id="sa_1",
            station_id="st_1",
            alias="fast",
            target_public_model_id="gpt-5.4-mini",
            fallback_target_public_model_id="",
            capability="chat/completions",
            status="active",
            is_default_text=1,
            is_default_image=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        price = SimpleNamespace(
            id="sp_1",
            station_id="st_1",
            station_alias_id="sa_1",
            billable_sku="legacy-gpt-5.4-mini-text",
            usage_unit_type="tokens",
            retail_input_per_million_cents=120,
            retail_output_per_million_cents=720,
            retail_price_per_image_cents=0.0,
            min_allowed_cents=450,
            max_allowed_cents=0,
            price_version=2,
            status="active",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        target = SimpleNamespace(
            public_id="gpt-5.4-mini",
            capabilities=("chat/completions", "responses"),
            billable_sku="legacy-gpt-5.4-mini-text",
            price_input_per_million=75,
            price_output_per_million=450,
            price_per_image_cents=0.0,
        )
        fake_db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(station),
                _ScalarOneOrNoneResult(branding),
                _AllResult([(alias, price)]),
            ]
        )

        with patch.object(stations_module.model_registry, "get_public_model", return_value=target):
            result = await stations_module.get_public_station("stone", db=fake_db)

        self.assertEqual(result["station"]["slug"], "stone")
        self.assertEqual(result["branding"]["display_name"], "Stone AI")
        self.assertEqual(result["aliases"][0]["id"], "fast")
        self.assertEqual(result["aliases"][0]["coincoin_price_input_per_million"], 120)
        self.assertEqual(result["aliases"][0]["coincoin_resolved_public_model"], "gpt-5.4-mini")

    async def test_update_station_branding_upserts_owner_branding(self):
        owner = SimpleNamespace(id="u_owner")
        station = SimpleNamespace(
            id="st_1",
            slug="stone",
            display_name="Stone Station",
            status="active",
            commission_rate=0.15,
            settlement_method="alipay_manual",
            settlement_payee_name="",
            settlement_payee_account="",
            settlement_qr_url="",
            created_at=datetime.utcnow(),
        )
        fake_db = _FakeDB(execute_results=[
            _ScalarOneOrNoneResult(None),
            _ScalarOneOrNoneResult(None),
        ])

        with patch.object(stations_module, "_get_current_user", AsyncMock(return_value=owner)), patch.object(
            stations_module, "_get_owned_station", AsyncMock(return_value=station)
        ):
            payload = stations_module.StationBrandingUpdateRequest(
                display_name="Stone AI",
                logo_url="https://cdn.example/logo.png",
                support_email="support@stone.example",
                docs_intro="Stone docs",
            )
            result = await stations_module.update_station_branding(payload, request=None, db=fake_db)

        self.assertTrue(result["success"])
        self.assertEqual(result["branding"]["display_name"], "Stone AI")
        self.assertEqual(result["branding"]["support_email"], "support@stone.example")
        self.assertEqual(station.display_name, "Stone AI")
        self.assertEqual(len(fake_db.added), 1)
        self.assertEqual(fake_db.commits, 1)

    async def test_create_station_alias_creates_alias_and_pricebook(self):
        owner = SimpleNamespace(id="u_owner")
        station = SimpleNamespace(
            id="st_1",
            default_text_alias="",
            default_image_alias="",
        )
        fake_db = _FakeDB(execute_results=[
            _ScalarOneOrNoneResult(None),
            _ScalarOneOrNoneResult(None),
        ])
        target = SimpleNamespace(
            public_id="gpt-5.4-mini",
            capabilities=("chat/completions", "responses"),
            billable_sku="legacy-gpt-5.4-mini-text",
            price_input_per_million=75,
            price_output_per_million=450,
            price_per_image_cents=0.0,
        )

        with patch.object(stations_module, "_get_current_user", AsyncMock(return_value=owner)), patch.object(
            stations_module, "_get_owned_station", AsyncMock(return_value=station)
        ), patch.object(stations_module.model_registry, "get_public_model", return_value=target):
            payload = stations_module.StationAliasCreateRequest(
                alias="fast",
                target_public_model_id="gpt-5.4-mini",
                capability="chat/completions",
                retail_input_per_million_cents=120,
                retail_output_per_million_cents=720,
                is_default_text=True,
            )
            result = await stations_module.create_station_alias(payload, request=None, db=fake_db)

        self.assertTrue(result["success"])
        self.assertEqual(result["alias"]["alias"], "fast")
        self.assertEqual(result["alias"]["target_public_model_id"], "gpt-5.4-mini")
        self.assertEqual(result["pricebook"]["retail_input_per_million_cents"], 120)
        self.assertEqual(result["pricebook"]["retail_output_per_million_cents"], 720)
        self.assertEqual(station.default_text_alias, "fast")
        self.assertEqual(len(fake_db.added), 2)
        self.assertEqual(fake_db.flushes, 1)
        self.assertEqual(fake_db.commits, 1)

    async def test_list_station_alias_targets_filters_video_models(self):
        owner = SimpleNamespace(id="u_owner")
        station = SimpleNamespace(id="st_1")
        fake_db = _FakeDB()
        text_target = SimpleNamespace(
            public_id="gpt-5.4-mini",
            owned_by="openai",
            capabilities=("chat/completions", "responses"),
            billable_sku="legacy-gpt-5.4-mini-text",
            price_input_per_million=75,
            price_output_per_million=450,
            price_per_image_cents=0.0,
        )
        video_target = SimpleNamespace(
            public_id="seedance-v2-720p",
            owned_by="bytedance",
            capabilities=("videos/generations",),
            billable_sku="seedance-v2-720p-video-task",
            price_input_per_million=0,
            price_output_per_million=0,
            price_per_image_cents=0.0,
        )

        with patch.object(stations_module, "_get_current_user", AsyncMock(return_value=owner)), patch.object(
            stations_module, "_get_owned_station", AsyncMock(return_value=station)
        ), patch.object(stations_module.model_registry, "list_public_models", return_value=[text_target, video_target]):
            result = await stations_module.list_station_alias_targets(request=None, db=fake_db)

        self.assertEqual([item["id"] for item in result["data"]], ["gpt-5.4-mini"])
        self.assertEqual(fake_db.added, [])
        self.assertEqual(fake_db.commits, 0)

    async def test_create_station_alias_rejects_video_capability_without_db_write(self):
        owner = SimpleNamespace(id="u_owner")
        station = SimpleNamespace(id="st_1", default_text_alias="", default_image_alias="")
        fake_db = _FakeDB()

        with patch.object(stations_module, "_get_current_user", AsyncMock(return_value=owner)), patch.object(
            stations_module, "_get_owned_station", AsyncMock(return_value=station)
        ):
            payload = stations_module.StationAliasCreateRequest(
                alias="video",
                target_public_model_id="seedance-v2-720p",
                capability="videos/generations",
            )
            with self.assertRaises(stations_module.HTTPException) as ctx:
                await stations_module.create_station_alias(payload, request=None, db=fake_db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "station video aliases are not supported yet")
        self.assertEqual(fake_db.added, [])
        self.assertEqual(fake_db.flushes, 0)
        self.assertEqual(fake_db.commits, 0)

    async def test_admin_create_station_creates_owner_link_branding_and_default_alias(self):
        snapshot = self._station_url_settings_snapshot()
        owner = SimpleNamespace(id="u_owner", username="owner", email="owner@example.com", status="active")
        target = SimpleNamespace(
            public_id="gpt-5.4-mini",
            capabilities=("chat/completions", "responses"),
            billable_sku="legacy-gpt-5.4-mini-text",
            price_input_per_million=75,
            price_output_per_million=450,
            price_per_image_cents=0.0,
        )
        fake_db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(owner),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
            ]
        )

        try:
            stations_module.settings.station_public_base_url = "https://coincoin.ai"
            stations_module.settings.station_portal_domain = ""
            stations_module.settings.station_api_domain = ""
            stations_module.settings.station_api_base_url = ""
            stations_module.settings.station_portal_path_prefix = "/s"
            stations_module.settings.station_api_path_prefix = "/v1"

            with patch.object(stations_module.model_registry, "get_public_model", return_value=target):
                payload = stations_module.AdminStationCreateRequest(
                    owner_user_id="u_owner",
                    display_name="Stone Station",
                    slug="stone",
                    create_default_alias=True,
                    default_alias="fast",
                    default_target_public_model_id="gpt-5.4-mini",
                    retail_input_per_million_cents=120,
                    retail_output_per_million_cents=720,
                )
                result = await stations_module.create_admin_station(payload, db=fake_db)
        finally:
            self._restore_station_url_settings(snapshot)

        self.assertTrue(result["success"])
        self.assertEqual(result["station"]["slug"], "stone")
        self.assertEqual(result["station"]["owner_user_id"], "u_owner")
        self.assertEqual(result["station"]["portal_url_mode"], "path")
        self.assertEqual(result["alias"]["alias"], "fast")
        self.assertEqual(result["pricebook"]["retail_input_per_million_cents"], 120)
        self.assertEqual(fake_db.flushes, 1)
        self.assertEqual(fake_db.commits, 1)
        self.assertEqual(len(fake_db.added), 5)
        station = fake_db.added[0]
        owner_link = fake_db.added[1]
        branding = fake_db.added[2]
        self.assertEqual(station.owner_user_id, "u_owner")
        self.assertEqual(station.default_text_alias, "fast")
        self.assertEqual(owner_link.user_id, "u_owner")
        self.assertEqual(branding.display_name, "Stone Station")

    async def test_admin_create_station_rejects_duplicate_slug(self):
        owner = SimpleNamespace(id="u_owner", username="owner", email="owner@example.com", status="active")
        fake_db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(owner),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult("st_existing"),
            ]
        )

        payload = stations_module.AdminStationCreateRequest(
            owner_user_id="u_owner",
            display_name="Stone Station",
            slug="stone",
        )
        with self.assertRaises(stations_module.HTTPException) as ctx:
            await stations_module.create_admin_station(payload, db=fake_db)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "station slug already exists")
        self.assertEqual(fake_db.commits, 0)
        self.assertEqual(fake_db.added, [])

    async def test_admin_create_station_rejects_owner_already_linked_to_station(self):
        owner = SimpleNamespace(id="u_owner", username="owner", email="owner@example.com", status="active")
        existing_link = SimpleNamespace(id="sclink_existing", station_id="st_other")
        fake_db = _FakeDB(
            execute_results=[
                _ScalarOneOrNoneResult(owner),
                _ScalarOneOrNoneResult(None),
                _ScalarOneOrNoneResult(existing_link),
            ]
        )

        payload = stations_module.AdminStationCreateRequest(
            owner_user_id="u_owner",
            display_name="Stone Station",
        )
        with self.assertRaises(stations_module.HTTPException) as ctx:
            await stations_module.create_admin_station(payload, db=fake_db)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "owner already belongs to a station")
        self.assertEqual(fake_db.commits, 0)
        self.assertEqual(fake_db.added, [])

    async def test_update_station_pricebook_rejects_price_below_target_cost(self):
        owner = SimpleNamespace(id="u_owner")
        station = SimpleNamespace(id="st_1")
        alias = SimpleNamespace(
            id="sa_1",
            station_id="st_1",
            target_public_model_id="gpt-5.4-mini",
            status="active",
        )
        price = SimpleNamespace(
            id="sp_1",
            station_id="st_1",
            station_alias_id="sa_1",
            retail_input_per_million_cents=120,
            retail_output_per_million_cents=720,
            retail_price_per_image_cents=0.0,
            price_version=1,
            status="active",
        )
        fake_db = _FakeDB(execute_results=[_ScalarOneOrNoneResult(price), _ScalarOneOrNoneResult(alias)])
        target = SimpleNamespace(
            public_id="gpt-5.4-mini",
            capabilities=("chat/completions", "responses"),
            price_input_per_million=75,
            price_output_per_million=450,
            price_per_image_cents=0.0,
        )

        with patch.object(stations_module, "_get_current_user", AsyncMock(return_value=owner)), patch.object(
            stations_module, "_get_owned_station", AsyncMock(return_value=station)
        ), patch.object(stations_module.model_registry, "get_public_model", return_value=target):
            payload = stations_module.StationPricebookUpdateRequest(retail_input_per_million_cents=40)
            with self.assertRaises(stations_module.HTTPException) as ctx:
                await stations_module.update_station_pricebook_entry("sp_1", payload, request=None, db=fake_db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "retail price below platform cost")

    def test_station_public_payload_includes_wildcard_station_urls(self):
        snapshot = self._station_url_settings_snapshot()
        try:
            stations_module.settings.station_public_base_url = "https://coincoin.ai"
            stations_module.settings.station_portal_domain = "station.coincoin.ai"
            stations_module.settings.station_api_domain = "api.coincoin.ai"
            stations_module.settings.station_api_base_url = ""
            stations_module.settings.station_portal_path_prefix = "/s"
            stations_module.settings.station_api_path_prefix = "/v1"

            payload = stations_module._station_public_payload(self._station_row_for_url_tests())
        finally:
            self._restore_station_url_settings(snapshot)

        self.assertEqual(payload["portal_url"], "https://stone.station.coincoin.ai")
        self.assertEqual(payload["api_base_url"], "https://stone.api.coincoin.ai/v1")
        self.assertEqual(payload["portal_url_mode"], "wildcard")
        self.assertEqual(payload["api_url_mode"], "wildcard")

    def test_station_public_payload_falls_back_to_shared_coincoin_domain(self):
        snapshot = self._station_url_settings_snapshot()
        try:
            stations_module.settings.station_public_base_url = "https://coincoin.ai/"
            stations_module.settings.station_portal_domain = ""
            stations_module.settings.station_api_domain = ""
            stations_module.settings.station_api_base_url = ""
            stations_module.settings.station_portal_path_prefix = "s"
            stations_module.settings.station_api_path_prefix = "v1"

            payload = stations_module._station_public_payload(self._station_row_for_url_tests())
        finally:
            self._restore_station_url_settings(snapshot)

        self.assertEqual(payload["portal_url"], "https://coincoin.ai/s/stone")
        self.assertEqual(payload["api_base_url"], "https://coincoin.ai/v1")
        self.assertEqual(payload["portal_url_mode"], "path")
        self.assertEqual(payload["api_url_mode"], "shared")

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
        api_key_row = fake_db.added[2]
        self.assertEqual(api_key_row.kind, "api")
        self.assertTrue(bool(api_key_row.encrypted_key))

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
            result = await station_settlement_module.create_station_commission_entry_for_confirmed_order(fake_db, order)
        finally:
            station_settlement_module.settings.station_payout_hold_days = original_hold

        self.assertTrue(hasattr(result, "entry"))
        self.assertTrue(hasattr(result, "created"))
        entry = result.entry
        self.assertTrue(result.created)
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
