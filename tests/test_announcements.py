import unittest
from datetime import datetime
from types import SimpleNamespace

import app.admin as admin_module
import app.openai_compat as openai_module


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsCollection:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarsCollection(self._rows)


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


class AnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_create_announcement_accepts_modal_fields(self):
        db = _FakeDB()
        payload = admin_module.AnnouncementCreate(
            title="进群再领 $30",
            content="加入 CoinCoin 微信群，联系管理员领取额外 $30 API 额度。",
            priority="info",
            display_type="modal",
            audience="signup",
            cta_label="复制微信号",
            cta_value="birdsync",
            image_url="/wechat-group-coincoin.jpg",
        )

        result = await admin_module.create_announcement(payload, db)

        self.assertEqual(result["status"], "active")
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.added), 1)
        ann = db.added[0]
        self.assertEqual(ann.display_type, "modal")
        self.assertEqual(ann.audience, "signup")
        self.assertEqual(ann.cta_label, "复制微信号")
        self.assertEqual(ann.cta_value, "birdsync")
        self.assertEqual(ann.image_url, "/wechat-group-coincoin.jpg")

    async def test_admin_update_announcement_updates_modal_fields(self):
        ann = SimpleNamespace(
            id="ann_1",
            title="旧标题",
            content="旧内容",
            priority="info",
            display_type="banner",
            audience="all",
            cta_label="",
            cta_value="",
            image_url="",
            status="active",
        )
        db = _FakeDB(execute_results=[_ScalarOneOrNoneResult(ann)])
        payload = admin_module.AnnouncementUpdate(
            title="进群再领 $30",
            display_type="modal",
            audience="signup",
            cta_label="复制微信号",
            cta_value="birdsync",
            image_url="/wechat-group-coincoin.jpg",
        )

        result = await admin_module.update_announcement("ann_1", payload, db)

        self.assertEqual(result["status"], "active")
        self.assertEqual(db.commits, 1)
        self.assertEqual(ann.title, "进群再领 $30")
        self.assertEqual(ann.display_type, "modal")
        self.assertEqual(ann.audience, "signup")
        self.assertEqual(ann.cta_label, "复制微信号")
        self.assertEqual(ann.cta_value, "birdsync")
        self.assertEqual(ann.image_url, "/wechat-group-coincoin.jpg")

    async def test_public_announcements_returns_modal_fields(self):
        created_at = datetime(2026, 5, 3, 12, 0, 0)
        ann = SimpleNamespace(
            id="ann_group",
            title="进群再领 $30",
            content="加入 CoinCoin 微信群，联系管理员领取额外 $30 API 额度。",
            priority="info",
            display_type="modal",
            audience="signup",
            cta_label="复制微信号",
            cta_value="birdsync",
            image_url="/wechat-group-coincoin.jpg",
            created_at=created_at,
        )
        db = _FakeDB(execute_results=[_ScalarsResult([ann])])

        result = await openai_module.list_announcements(db)

        self.assertEqual(result[0]["display_type"], "modal")
        self.assertEqual(result[0]["audience"], "signup")
        self.assertEqual(result[0]["cta_label"], "复制微信号")
        self.assertEqual(result[0]["cta_value"], "birdsync")
        self.assertEqual(result[0]["image_url"], "/wechat-group-coincoin.jpg")


if __name__ == "__main__":
    unittest.main()
