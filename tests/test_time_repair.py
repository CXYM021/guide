# -*- coding: utf-8 -*-
"""时钟自救：记录落在未来时，今日与连胜会卡住。

场景：用户把设备时间前拨了 40 天，记录了一次练习，然后把时间改回来。
这条记录的 at 在未来，「今天」永远小于它所在的日历日，于是：

- today_xp 恒为 0；
- advance_streak 看到 gap < 0，判定为 same_day，连胜停在原地；
- 衰减计算里 now - last_practice_at 为负。

修复只改时间戳，不改经验、次数、质量统计，然后按新时间重建连胜。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

from engine.proficiency import (  # noqa: E402
    MemoryStorage, PowerRule, ProficiencySystem, SkillConfig)

BASE = 1_760_000_000
DAY = 86_400


def _system() -> ProficiencySystem:
    system = ProficiencySystem(MemoryStorage())
    system.register_skill("英语口语", SkillConfig(
        rule=PowerRule(base=100, exponent=1.5), max_level=8))
    return system


def _practice(system: ProficiencySystem, at: int) -> None:
    system.add_practice(
        "英语口语", difficulty=3, quality=85, feedback="measured",
        task_key=f"t{at}", metrics={
            "continuous_minutes": 8, "active_phrases": 6, "listener_score": 72,
        }, now=at)


class TestFutureEvents(unittest.TestCase):
    def test_future_event_is_reported_and_blocks_today(self):
        system = _system()
        _practice(system, BASE - DAY * 2)          # 两天前的一次
        _practice(system, BASE + DAY * 40)         # 设备时间被前拨后的一次
        info = system.future_events(now=BASE)
        self.assertEqual(info["count"], 1)
        self.assertEqual(info["ahead_days"], 40)
        # 今日所得仍是 0：那条未来记录不在「今天」
        stats = system.stats_summary(days=3, now=BASE)
        self.assertEqual(stats["series"][-1]["xp"], 0)

    def test_repair_moves_time_but_keeps_experience(self):
        system = _system()
        _practice(system, BASE - DAY * 2)
        _practice(system, BASE + DAY * 40)
        before = system.list_progress(now=BASE)["英语口语"].to_dict()
        out = system.repair_future_timestamps(now=BASE)
        self.assertEqual(out["changed"], 1)
        after = system.list_progress(now=BASE)["英语口语"].to_dict()
        self.assertEqual(after["total_exp"], before["total_exp"])
        self.assertEqual(after["valid_sessions"], before["valid_sessions"])
        self.assertEqual(after["level"], before["level"])
        # 修完之后，两条记录都落在今天或之前，今日所得不再为 0
        self.assertEqual(system.future_events(now=BASE)["count"], 0)
        self.assertGreater(system.stats_summary(days=3, now=BASE)["series"][-1]["xp"], 0)
        # 连胜按新时间重建：至少 1 天，且最后一天就是今天
        streak = system.streak_info(now=BASE)
        self.assertEqual(streak["last_day"], streak["today_day"])
        self.assertGreaterEqual(streak["current_days"], 1)

    def test_repair_is_idempotent(self):
        system = _system()
        _practice(system, BASE + DAY * 5)
        self.assertEqual(system.repair_future_timestamps(now=BASE)["changed"], 1)
        self.assertEqual(system.repair_future_timestamps(now=BASE)["changed"], 0)

    def test_clean_archive_reports_nothing(self):
        system = _system()
        _practice(system, BASE - DAY)
        info = system.future_events(now=BASE)
        self.assertEqual(info["count"], 0)
        self.assertEqual(info["ahead_days"], 0)


if __name__ == "__main__":
    unittest.main()
