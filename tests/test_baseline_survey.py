# -*- coding: utf-8 -*-
"""分技能首登自评的回归测试。

口径（改题库之前先读）：
1. 每个内置技能一套题，题目问的是可对照外部标准的事实（CEFR/DELE/VDOT/
   相对力量/作品类型），不是「你觉得自己怎么样」。
2. 自评最高给到 LV6，而且 LV6 需要至少 3 题落在该题最高档的 80% 以上；
   否则降到 LV5——防止「一题拉满、其余空着」也拿最高档。
3. 自建技能没有专属题库，退回通用四问，最高 LV3。
4. 自评只抬等级下限，不写 total_exp；已经有练习记录的技能不能再改起点。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

from engine import surveys  # noqa: E402

try:
    from test_multitenant import ProductHarness
except ImportError:  # pragma: no cover
    from tests.test_multitenant import ProductHarness  # type: ignore

SPEAKING_EXPERT = {
    "cefr": "c2", "exam": "top", "sustain": "2m", "use": "lead", "listener": "native",
}
SPEAKING_B2 = {
    "cefr": "b2", "exam": "mid", "sustain": "1m", "use": "work", "listener": "accent",
}
GENERIC_TOP = {
    "years": "gt3", "frequency": "daily", "feedback": "measured", "transfer": "often",
}


class TestBaselineScoring(unittest.TestCase):
    def test_every_builtin_skill_has_its_own_spec(self):
        specs = surveys.baseline_specs()
        for skill in ("英语口语", "英语读写", "西班牙语", "跑步技术",
                      "健身技术", "电脑办公软件技术", "剪辑技术"):
            self.assertIn(skill, specs)
            self.assertEqual(specs[skill]["max_level"], 6)
            self.assertGreaterEqual(len(specs[skill]["questions"]), 5)
            self.assertTrue(specs[skill]["framework"])
        self.assertEqual(specs["__default__"]["max_level"], 3)

    def test_expert_answers_reach_top_self_report_level(self):
        got = surveys.baseline_level(SPEAKING_EXPERT, "英语口语")
        self.assertEqual(got["level"], 6)
        self.assertEqual(got["max_level"], 6)
        self.assertFalse(got["capped_by_anchors"])
        self.assertIn("CEFR", got["framework"])

    def test_mid_answers_land_in_the_middle(self):
        self.assertEqual(surveys.baseline_level(SPEAKING_B2, "英语口语")["level"], 4)
        self.assertEqual(
            surveys.baseline_level(
                {"cefr": "a1", "exam": "none", "sustain": "no",
                 "use": "none", "listener": "repeat"}, "英语口语")["level"], 1)

    def test_top_level_requires_several_strong_anchors(self):
        # 只把一题拉满、其余最低：总分够不上，不能只靠单题。
        lopsided = {"cefr": "c2", "exam": "none", "sustain": "no",
                    "use": "none", "listener": "repeat"}
        got = surveys.baseline_level(lopsided, "英语口语")
        self.assertLess(got["level"], 6)
        # 几乎所有题都高、但差一档：落在 85% 附近时由锚点规则决定。
        near = {"cefr": "c2", "exam": "high", "sustain": "2m",
                "use": "work", "listener": "fine"}
        got = surveys.baseline_level(near, "英语口语")
        self.assertIn(got["level"], (5, 6))
        if got["level"] == 5:
            self.assertTrue(got["capped_by_anchors"] or got["percent"] < 85.0)

    def test_generic_fallback_caps_at_three(self):
        got = surveys.baseline_level(GENERIC_TOP)
        self.assertEqual(got["level"], 3)
        got = surveys.baseline_level(GENERIC_TOP, "自建技能X")
        self.assertEqual(got["level"], 3)
        self.assertEqual(got["max_level"], 3)

    def test_unknown_value_is_rejected(self):
        bad = dict(SPEAKING_EXPERT, cefr="d9")
        with self.assertRaises(ValueError):
            surveys.baseline_level(bad, "英语口语")
        with self.assertRaises(ValueError):
            surveys.baseline_level({}, "英语口语")


class TestBaselineApi(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.app = ProductHarness(Path(self.td.name))
        code, _, headers = self.app.req(
            "/api/auth/register",
            {"username": "Eve", "password": "password123"})
        self.assertEqual(code, 201)
        self.cookie = headers.get("Set-Cookie", "").split(";")[0]

    def tearDown(self) -> None:
        self.app.close()
        self.td.cleanup()

    def test_surveys_payload_has_per_skill_sets(self):
        code, res, _ = self.app.req("/api/surveys", cookie=self.cookie)
        self.assertEqual(code, 200, res)
        self.assertIn("英语口语", res["baseline_by_skill"])
        self.assertIn("剪辑技术", res["baseline_by_skill"])
        self.assertEqual(res["baseline_by_skill"]["跑步技术"]["max_level"], 6)
        self.assertEqual(res["baseline_max_level"], 6)
        self.assertEqual(len(res["session"]), 4)

    def test_baseline_sets_credit_floor_without_xp(self):
        code, res, _ = self.app.req(
            "/api/baseline",
            {"skill_id": "英语口语", "answers": SPEAKING_EXPERT},
            cookie=self.cookie)
        self.assertEqual(code, 200, res)
        self.assertEqual(res["baseline_result"]["level"], 6)
        row = next(r for r in res["skills"] if r["skill_id"] == "英语口语")
        self.assertEqual(row["level"], 6)
        self.assertEqual(row["baseline_level"], 6)
        self.assertEqual(row["total_exp"], 0)

    def test_baseline_rejects_after_practice(self):
        body = {
            "skill_id": "英语口语", "difficulty": 2, "quality": 80,
            "feedback": "external", "task_key": "free talk",
            "metrics": {"continuous_minutes": 8, "active_phrases": 6,
                        "listener_score": 72},
        }
        code, res, _ = self.app.req("/api/practice", body, cookie=self.cookie)
        self.assertEqual(code, 200, res)
        code, res, _ = self.app.req(
            "/api/baseline",
            {"skill_id": "英语口语", "answers": SPEAKING_EXPERT},
            cookie=self.cookie)
        self.assertEqual(code, 400, res)
        self.assertIn("练习记录", str(res))

    def test_custom_skill_baseline_uses_its_own_metrics(self):
        """自建技能没有外部考试，但用户填过指标，所以问的是指标档位。"""
        code, res, _ = self.app.req(
            "/api/skills/custom/add",
            {"name": "口琴", "metrics": [{"key": "minutes", "label": "分钟",
                                          "unit": "分钟", "range_low": 1,
                                          "range_high": 240}]},
            cookie=self.cookie)
        self.assertEqual(code, 200, res)
        code, res, _ = self.app.req("/api/surveys", cookie=self.cookie)
        self.assertEqual(code, 200, res)
        spec = res["baseline_by_skill"]["口琴"]
        self.assertEqual(spec["max_level"], 6)
        self.assertEqual(spec["questions"][0]["key"], "minutes")
        code, res, _ = self.app.req(
            "/api/baseline", {"skill_id": "口琴", "answers": {"minutes": "lv5"}},
            cookie=self.cookie)
        self.assertEqual(code, 200, res)
        self.assertEqual(res["baseline_result"]["level"], 5)
        # 取最弱的一项：两指标时以低者为准
        code, res, _ = self.app.req(
            "/api/skills/custom/add",
            {"name": "吹口琴长音", "metrics": [
                {"key": "seconds", "label": "长音", "unit": "秒",
                 "range_low": 1, "range_high": 300},
                {"key": "songs", "label": "曲目", "unit": "首",
                 "range_low": 0, "range_high": 200}]},
            cookie=self.cookie)
        self.assertEqual(code, 200, res)
        code, res, _ = self.app.req(
            "/api/baseline",
            {"skill_id": "吹口琴长音",
             "answers": {"seconds": "lv6", "songs": "none"}},
            cookie=self.cookie)
        self.assertEqual(code, 200, res)
        self.assertEqual(res["baseline_result"]["level"], 1)


if __name__ == "__main__":
    unittest.main()
