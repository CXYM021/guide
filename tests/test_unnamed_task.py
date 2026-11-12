# -*- coding: utf-8 -*-
"""「不填任务标识」不该被当成「同一个任务」的回归测试。

真实习惯：多数人打开记录抽屉只填难度/质量就提交，任务标识留空。
旧实现把空值统一写成「未命名任务」，于是：
  第 2 次 → 重复折扣（收益 ×0.2）
  第 3 次起 → REPEATED_TASK，永久无效（因为窗口里一直是同一个键）
这会让最省事的用户拿到最差的结果。现在：没命名 = 不判定重复。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

try:
    from test_multitenant import ProductHarness
except ImportError:  # pragma: no cover
    from tests.test_multitenant import ProductHarness  # type: ignore

METRICS = {"continuous_minutes": 8, "active_phrases": 6, "listener_score": 72}


class TestUnnamedTask(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.app = ProductHarness(Path(self.td.name))
        code, _, headers = self.app.req(
            "/api/auth/register",
            {"username": "Bob", "password": "password123"})
        self.assertEqual(code, 201)
        self.cookie = headers.get("Set-Cookie", "").split(";")[0]

    def tearDown(self) -> None:
        self.app.close()
        self.td.cleanup()

    def practice(self, task_key, quality=85):
        body = {
            "skill_id": "英语口语", "difficulty": 3, "quality": quality,
            "feedback": "measured", "task_key": task_key, "metrics": METRICS,
        }
        code, res, _ = self.app.req("/api/practice", body, cookie=self.cookie)
        self.assertEqual(code, 200, res)
        return res["verdict"]

    def test_blank_task_key_is_never_rejected_as_repeat(self):
        for i in range(4):
            verdict = self.practice("")
            self.assertTrue(
                verdict["valid"],
                f"第 {i + 1} 次未命名练习被判为 {verdict['reason_code']}")
            self.assertFalse(verdict["factors"]["repeat"])

    def test_named_repeat_still_discounted_and_gated(self):
        first = self.practice("跟读 20 分钟")
        second = self.practice("跟读 20 分钟")
        third = self.practice("跟读 20 分钟")
        self.assertTrue(first["valid"])
        self.assertTrue(second["valid"])
        self.assertTrue(second["factors"]["repeat"])
        self.assertLess(second["gain"], first["gain"])
        self.assertFalse(third["valid"])
        self.assertEqual(third["reason_code"], "REPEATED_TASK")

    def test_different_names_are_not_repeats(self):
        a = self.practice("跟读 A 段")
        b = self.practice("跟读 B 段")
        self.assertTrue(b["valid"])
        self.assertFalse(b["factors"]["repeat"])
        self.assertGreater(b["gain"], 0)
        self.assertGreater(a["gain"], 0)


if __name__ == "__main__":
    unittest.main()
