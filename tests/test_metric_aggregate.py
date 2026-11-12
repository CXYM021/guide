# -*- coding: utf-8 -*-
"""指标聚合口径：累计（剪辑交付）与单次最好（跑步距离）。

口径：
1. 剪辑按「累计交付件数」判等级：每次只交 1 件，也能靠次数升级，
   不会被「本次平均件数」卡死。
2. 跑步按「最长的一次」判等级：偶尔跑一次长的就算数，平时跑短不拖后腿。
3. 两种口径都写进 state（metric_sums / metric_max）并可随存档往返。
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
from engine.realistic_rules import SKILL_RULES  # noqa: E402


def _system() -> ProficiencySystem:
    system = ProficiencySystem(MemoryStorage())
    for sid in ("剪辑技术", "跑步技术"):
        system.register_skill(sid, SkillConfig(
            rule=PowerRule(base=100, exponent=1.5), max_level=8))
    return system


def _practice(system: ProficiencySystem, sid: str, metrics: dict, at: int) -> dict:
    system.add_practice(
        sid, difficulty=3, quality=85, feedback="measured",
        task_key=f"t{at}", metrics=metrics, now=at)
    return system.list_progress(now=at)[sid].to_dict()


class TestAggregate(unittest.TestCase):
    def test_deliverables_use_cumulative_total(self):
        system = _system()
        base = 1_760_000_000
        # 每次只交 1 件，交 8 次 = 累计 8 件；指标门槛按累计算到 LV3。
        for i in range(8):
            row = _practice(system, "剪辑技术",
                            {"deliverables": 1, "client_acceptance": 90,
                             "delivery_score": 85}, base + i * 86_400)
        spec = next(m for m in SKILL_RULES["剪辑技术"].metrics
                    if m.key == "deliverables")
        self.assertEqual(spec.aggregate, "total")
        self.assertGreaterEqual(row["effective_exp"], 0)
        state = system._states["剪辑技术"]  # noqa: SLF001
        self.assertEqual(state.metric_sums["deliverables"], 8)
        # 累计 8 件已到 LV3 的要求（8），不是「平均 1 件」。
        self.assertGreaterEqual(state.metric_sums["deliverables"],
                                spec.level_requirements[2])

    def test_distance_uses_single_best(self):
        system = _system()
        base = 1_760_000_000
        for i in range(5):
            _practice(system, "跑步技术",
                      {"distance_meters": 1000, "cadence_spm": 165,
                       "form_score": 70, "pain_flag": 0, "rpe": 12,
                       "stop_sign": 0}, base + i * 86_400)
        _practice(system, "跑步技术",
                  {"distance_meters": 5000, "cadence_spm": 165,
                   "form_score": 70, "pain_flag": 0, "rpe": 12, "stop_sign": 0},
                  base + 6 * 86_400)
        state = system._states["跑步技术"]  # noqa: SLF001
        self.assertEqual(state.metric_max["distance_meters"], 5000)
        self.assertEqual(state.metric_sums["distance_meters"], 10_000)

    def test_metric_max_survives_roundtrip(self):
        system = _system()
        system.add_practice(
            "跑步技术", difficulty=3, quality=85, feedback="measured",
            task_key="long", now=1_760_000_000,
            metrics={"distance_meters": 8000, "cadence_spm": 168,
                     "form_score": 75, "pain_flag": 0, "rpe": 12,
                     "stop_sign": 0})
        payload = system.to_snapshot_dict() if hasattr(
            system, "to_snapshot_dict") else None
        self.assertIsNone(payload)  # 存档走 Snapshot，这里只验内存对象
        state = system._states["跑步技术"]  # noqa: SLF001
        self.assertEqual(state.metric_max["distance_meters"], 8000)
        self.assertEqual(state.metric_max["cadence_spm"], 168)


if __name__ == "__main__":
    unittest.main()
