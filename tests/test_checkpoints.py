# -*- coding: utf-8 -*-
"""等级验证题（语言类 LV5+）与技能归档的回归测试。

口径：
1. 只有三个语言技能有题库，LV5 起「指标够 + 经验够」之外还要过一遍客观题；
   答错不扣经验，可以重做。
2. 题面接口不下发答案与解析。
3. 归档只是从今日主线/推荐里摘掉，不清历史；可以恢复。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

from engine import checkpoints  # noqa: E402

try:
    from test_multitenant import ProductHarness
except ImportError:  # pragma: no cover
    from tests.test_multitenant import ProductHarness  # type: ignore


def _answers(skill_id):
    return {q["id"]: q["answer"] for q in checkpoints._BANKS[skill_id]["items"]}


class TestQuizBanks(unittest.TestCase):
    def test_only_language_skills_have_banks(self):
        self.assertEqual(checkpoints.quiz_level("英语口语"), 5)
        self.assertEqual(checkpoints.quiz_level("英语读写"), 5)
        self.assertEqual(checkpoints.quiz_level("西班牙语"), 5)
        self.assertIsNone(checkpoints.quiz_level("跑步技术"))
        self.assertIsNone(checkpoints.quiz_level("自建技能"))

    def test_spec_hides_answers(self):
        spec = checkpoints.spec("英语读写")
        self.assertEqual(len(spec["items"]), 3)
        for item in spec["items"]:
            self.assertNotIn("answer", item)
            self.assertNotIn("why", item)
            self.assertGreaterEqual(len(item["options"]), 3)

    def test_grade_requires_every_answer(self):
        right = _answers("西班牙语")
        self.assertTrue(checkpoints.grade("西班牙语", right)["passed"])
        partial = dict(right)
        partial.pop(next(iter(partial)))
        self.assertFalse(checkpoints.grade("西班牙语", partial)["passed"])
        wrong = {k: (v + 1) % 4 for k, v in right.items()}
        got = checkpoints.grade("西班牙语", wrong)
        self.assertFalse(got["passed"])
        self.assertEqual(got["correct"], 0)


class TestQuizGate(unittest.TestCase):
    """LV5 起：经验到了、指标够了，没有过验证题也不许升级。"""

    def _system(self):
        from engine.proficiency import (
            MemoryStorage, PowerRule, ProficiencySystem, SkillConfig)
        system = ProficiencySystem(MemoryStorage())
        system.register_skill("英语口语", SkillConfig(
            rule=PowerRule(base=100, exponent=1.5), max_level=8))
        system.add_exp("英语口语", 2822)      # 经验到 LV6
        state = system._states["英语口语"]      # noqa: SLF001 - 断言内部状态
        state.level = 5                        # 模拟自评起点 LV5
        state.metric_sums = {
            "continuous_minutes": 40 * 50, "active_phrases": 30 * 50,
            "listener_score": 95 * 50}
        state.valid_sessions = 50
        state.quality_sum = 4500
        state.feedback_max = 3
        state.max_difficulty = 5
        return system, state

    def test_level_six_needs_a_passed_quiz(self):
        system, state = self._system()
        self.assertEqual(system._granted_level("英语口语", state, system._now(None)), 5)  # noqa: SLF001
        state.verified_level = 6
        self.assertEqual(system._granted_level("英语口语", state, system._now(None)), 6)  # noqa: SLF001

    def test_non_language_skill_is_not_gated(self):
        from engine.checkpoints import quiz_level
        self.assertIsNone(quiz_level("跑步技术"))


class TestArchiveApi(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.app = ProductHarness(Path(self.td.name))
        code, _, headers = self.app.req(
            "/api/auth/register", {"username": "Arch", "password": "password123"})
        self.assertEqual(code, 201)
        self.cookie = headers.get("Set-Cookie", "").split(";")[0]

    def tearDown(self) -> None:
        self.app.close()
        self.td.cleanup()

    def _row(self, res, skill_id):
        return next(r for r in res["skills"] if r["skill_id"] == skill_id)

    def test_archive_hides_from_today_but_keeps_history(self):
        code, res, _ = self.app.req(
            "/api/skills/archive", {"skill_id": "跑步技术", "archived": True},
            cookie=self.cookie)
        self.assertEqual(code, 200, res)
        row = self._row(res, "跑步技术")
        self.assertTrue(row["archived"])
        self.assertNotIn("跑步技术", res["guidance"]["missing"])
        code, res, _ = self.app.req(
            "/api/skills/archive", {"skill_id": "跑步技术", "archived": False},
            cookie=self.cookie)
        self.assertEqual(code, 200, res)
        self.assertFalse(self._row(res, "跑步技术")["archived"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
