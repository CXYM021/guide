# -*- coding: utf-8 -*-
"""好友：只读摘要、双向关系、真人护道、缺好友时的诚实提示。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

try:
    from test_multitenant import ProductHarness  # noqa: E402
except ImportError:  # pragma: no cover
    from tests.test_multitenant import ProductHarness  # type: ignore

import friends as F  # noqa: E402
from world_state import WorldState  # noqa: E402

DAY = 86_400


class TestPracticeSummary(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def write(self, payload):
        (self.dir / "skills.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_missing_file_is_unknown(self):
        self.assertFalse(F.practice_summary(self.dir, now=1000)["known"])

    def test_empty_events(self):
        self.write({"events": []})
        s = F.practice_summary(self.dir, now=1000)
        self.assertTrue(s["known"])
        self.assertIsNone(s["last_at"])
        self.assertFalse(s["practiced_today"])
        self.assertFalse(F.needs_a_nudge(s))

    def test_practiced_today_needs_no_nudge(self):
        now = 1_700_000_000
        self.write({"events": [{"at": now - 60}]})
        s = F.practice_summary(self.dir, now=now)
        self.assertTrue(s["practiced_today"])
        self.assertFalse(F.needs_a_nudge(s))

    def test_two_days_ago_needs_nudge(self):
        now = 1_700_000_000
        self.write({"events": [{"at": now - 2 * DAY - 60}]})
        s = F.practice_summary(self.dir, now=now)
        self.assertFalse(s["practiced_today"])
        self.assertGreaterEqual(s["days_since"], 2)
        self.assertTrue(F.needs_a_nudge(s))

    def test_broken_json_does_not_raise(self):
        (self.dir / "skills.json").write_text("{not json", encoding="utf-8")
        self.assertFalse(F.practice_summary(self.dir, now=1)["known"])


class TestFriendStore(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = F.FriendStore(Path(self.td.name))

    def tearDown(self):
        self.td.cleanup()

    def test_add_is_bidirectional_and_idempotent(self):
        self.store.add("a", "alice", "b", "bob", now=100)
        self.store.add("a", "alice", "b", "bob", now=200)
        self.assertIn("b", self.store.links_of("a"))
        self.assertIn("a", self.store.links_of("b"))
        self.assertEqual(len(self.store.links_of("a")), 1)

    def test_cannot_add_self(self):
        with self.assertRaises(F.FriendError):
            self.store.add("a", "alice", "a", "alice")

    def test_remove_both_sides(self):
        self.store.add("a", "alice", "b", "bob", now=100)
        self.store.remove("a", "b")
        self.assertEqual(self.store.links_of("a"), {})
        self.assertEqual(self.store.links_of("b"), {})

    def test_limit(self):
        for i in range(F.MAX_FRIENDS):
            self.store.add("a", "alice", f"u{i}", f"n{i}", now=1)
        with self.assertRaises(F.FriendError):
            self.store.add("a", "alice", "overflow", "x", now=1)

    def test_survives_reload(self):
        self.store.add("a", "alice", "b", "bob", now=100)
        again = F.FriendStore(Path(self.td.name))
        self.assertIn("b", again.links_of("a"))


class TestGuardExternal(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.world = WorldState(Path(self.td.name) / "world.json", created_at=0)

    def tearDown(self):
        self.td.cleanup()

    def test_requires_risk(self):
        res = self.world.guard_external("friend:x", now=1000, at_risk=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason_code"], "TARGET_NOT_AT_RISK")

    def test_success_then_duplicate(self):
        res = self.world.guard_external("friend:x", now=1000, at_risk=True,
                                        note="给好友「bob」护道")
        self.assertTrue(res["ok"], res)
        self.assertGreater(res["reputation"], 0)
        again = self.world.guard_external("friend:x", now=1100, at_risk=True)
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason_code"], "DUPLICATE")

    def test_does_not_touch_preset_actors(self):
        before = dict(self.world.snapshot(now=1000)["actors"][0])
        self.world.guard_external("friend:x", now=1000, at_risk=True)
        after = dict(self.world.snapshot(now=1000)["actors"][0])
        self.assertEqual(before.get("protected_days"), after.get("protected_days"))


class TestFriendApi(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.td.name)
        self.app = ProductHarness(self.data_dir)
        _, alice, self.cookie_a = self.app.register("alice")
        _, bob, self.cookie_b = self.app.register("bob")
        self.alice_id, self.bob_id = alice["user"]["id"], bob["user"]["id"]

    def tearDown(self):
        self.app.close()
        self.td.cleanup()

    def test_requires_login(self):
        code, body, _ = self.app.req("/api/friends")
        self.assertEqual(code, 401, body)

    def test_add_and_see_each_other(self):
        code, body, _ = self.app.req("/api/friends/add",
                                     {"username": "bob"}, cookie=self.cookie_a)
        self.assertEqual(code, 200, body)
        self.assertEqual([f["name"] for f in body["friends"]], ["bob"])
        code, other, _ = self.app.req("/api/friends", cookie=self.cookie_b)
        self.assertEqual(code, 200, other)
        self.assertEqual([f["name"] for f in other["friends"]], ["alice"])

    def test_add_unknown_user_says_so(self):
        code, body, _ = self.app.req("/api/friends/add",
                                     {"username": "nobody-here"}, cookie=self.cookie_a)
        self.assertEqual(code, 400, body)
        self.assertIn("没找到这个用户名", body["error"])

    def test_add_self_is_rejected(self):
        code, body, _ = self.app.req("/api/friends/add",
                                     {"username": "alice"}, cookie=self.cookie_a)
        self.assertEqual(code, 400, body)

    def test_remove(self):
        self.app.req("/api/friends/add", {"username": "bob"}, cookie=self.cookie_a)
        code, body, _ = self.app.req("/api/friends/remove",
                                     {"uid": self.bob_id}, cookie=self.cookie_a)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["friends"], [])

    def test_guard_needs_a_real_gap(self):
        self.app.req("/api/friends/add", {"username": "bob"}, cookie=self.cookie_a)
        code, body, _ = self.app.req("/api/friends/guard",
                                     {"uid": self.bob_id}, cookie=self.cookie_a)
        self.assertEqual(code, 400, body)
        self.assertIn("还没开始练", body["error"])

    def test_guard_after_two_idle_days_grants_reputation(self):
        self.app.req("/api/friends/add", {"username": "bob"}, cookie=self.cookie_a)
        # 造一条两三天前的练习记录，代表「bob 今天没练」
        stamp = int(time.time()) - 3 * DAY
        path = self.data_dir / "users" / self.bob_id / "skills.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"events": [{"at": stamp, "task_key": "x"}]}),
                        encoding="utf-8")
        code, body, _ = self.app.req("/api/friends/guard",
                                     {"uid": self.bob_id}, cookie=self.cookie_a)
        self.assertEqual(code, 200, body)
        self.assertGreater(body["reputation"], 0)
        again = self.app.req("/api/friends/guard", {"uid": self.bob_id},
                             cookie=self.cookie_a)[1]
        self.assertIn("护过道", again.get("error", ""))


if __name__ == "__main__":
    unittest.main()
