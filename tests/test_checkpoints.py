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
        self.assertEqual(len(spec["items"]), 6)
        for item in spec["items"]:
            self.assertNotIn("answer", item)
            self.assertNotIn("why", item)
            self.assertGreaterEqual(len(item["options"]), 3)

    def test_each_attempt_gets_three_unseen_questions(self):
        first = [q["id"] for q in checkpoints.pick("英语读写")]
        self.assertEqual(len(first), 3)
        second = [q["id"] for q in checkpoints.pick("英语读写", first)]
        self.assertEqual(len(second), 3)
        self.assertFalse(set(first) & set(second), "两轮题目不该重复")

    def test_grade_counts_only_answered_questions(self):
        three = [q for q in checkpoints._BANKS["西班牙语"]["items"][:3]]
        answers = {q["id"]: q["answer"] for q in three}
        got = checkpoints.grade_answers("西班牙语", answers)
        self.assertEqual(got["correct"], 3)
        answered_one = {three[0]["id"]: three[0]["answer"]}
        got = checkpoints.grade_answers("西班牙语", answered_one)
        self.assertEqual(got["correct"], 1)
        self.assertEqual(got["answered"], 1)
        wrong = {q["id"]: (q["answer"] + 1) % 4 for q in three}
        self.assertEqual(checkpoints.grade_answers("西班牙语", wrong)["correct"], 0)


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


    def test_quiz_credits_accumulate_across_attempts(self):
        system, state = self._system()
        state.quiz_retry_after = -1          # 允许立刻答题（单元测试绕开「再练一次」）
        first = system.record_quiz("英语口语", ["sp1", "sp2"])   # 2 题
        self.assertFalse(first["passed"])
        self.assertEqual(first["credit"], 2)
        # 答错后必须再练一次才会重新出题
        self.assertGreater(state.quiz_retry_after, 0)
        self.assertFalse(system.list_progress(now=system._now(None))["英语口语"].quiz_required)  # noqa: SLF001
        # 再练一次后放行，继续累计第 3 题 → 通过并记 verified_level
        system.add_practice(
            "英语口语", difficulty=4, quality=85, feedback="measured",
            task_key="again", metrics={"continuous_minutes": 45,
                                       "active_phrases": 32,
                                       "listener_score": 96})
        second = system.record_quiz("英语口语", ["sp3"])
        self.assertTrue(second["passed"])
        self.assertEqual(state.verified_level, 6)
        self.assertEqual(state.quiz_credit, 0)

    def test_wrong_answers_do_not_change_level_or_exp(self):
        system, state = self._system()
        state.quiz_retry_after = -1
        before = (state.total_exp, state.level)
        out = system.record_quiz("英语口语", [])
        self.assertFalse(out["passed"])
        self.assertEqual((state.total_exp, state.level), before)
        self.assertTrue(state.quiz_seen == [])

    def test_seen_questions_rotate_even_when_all_wrong(self):
        """答错的题也要记进 seen，否则下次还是同一套题。"""
        system, state = self._system()
        state.quiz_retry_after = -1
        system.record_quiz("英语口语", [], attempted_ids=["sp1", "sp2", "sp3"])
        self.assertEqual(state.quiz_seen, ["sp1", "sp2", "sp3"])
        picked = checkpoints.pick("英语口语", seen=system.quiz_seen_ids("英语口语"))
        self.assertFalse({q["id"] for q in picked} & {"sp1", "sp2", "sp3"})

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
