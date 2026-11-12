# -*- coding: utf-8 -*-
"""基准任务复测：给「我到底有没有变强」留一条可复核的证据线。

边界（必须先读）
----------------
1. 本机不验真。`feedback="measured"`（客观度量）只是【本人自报的反馈类型】，
   系统不接收图片、不下载链接、不校验成绩单真伪，trusted 恒为 False。
   所以这里提供的是「同一口径下的纵向对照」，不是外部权威认证。
2. 复测**不写经验、不改保持度、不绕过任何判定门**。经验只由
   proficiency.add_practice 决定；保持度是纯函数，回补会破坏可复现性，
   也会推翻「长期搁置自然生疏」的已拍板规则。
3. 基准必须锚定一条真实的有效练习事件（event.seq），不能凭空声明。
4. 口径锁死：复测的指标字段集合必须与基准一致，任务名必须相同，
   反馈必须自报为客观度量，且间隔达到最小天数。任何一项不满足就不记账，
   也不会产生「通过」。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from realistic_rules import MEDICAL_KEYS, skill_rule_for

SCHEMA = 1
DEFAULT_MIN_GAP_DAYS = 28
REQUIRED_FEEDBACK = "measured"


def _require_int(value: object, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} 必须是 >= {minimum} 的整数")
    return value


@dataclass
class BenchmarkCard:
    id: str
    skill_id: str
    task_key: str
    metric_key: str
    metric_label: str
    unit: str
    metric_keys: tuple[str, ...]
    baseline: int
    baseline_event_seq: int
    created_at: int
    min_gap_days: int = DEFAULT_MIN_GAP_DAYS
    require_feedback: str = REQUIRED_FEEDBACK
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id, "skill_id": self.skill_id,
            "task_key": self.task_key, "metric_key": self.metric_key,
            "metric_label": self.metric_label, "unit": self.unit,
            "metric_keys": list(self.metric_keys),
            "baseline": self.baseline,
            "baseline_event_seq": self.baseline_event_seq,
            "created_at": self.created_at,
            "min_gap_days": self.min_gap_days,
            "require_feedback": self.require_feedback,
            "active": self.active,
        }

    @staticmethod
    def from_dict(d: dict) -> "BenchmarkCard":
        return BenchmarkCard(
            id=str(d["id"]), skill_id=str(d["skill_id"]),
            task_key=str(d["task_key"]), metric_key=str(d["metric_key"]),
            metric_label=str(d["metric_label"]), unit=str(d.get("unit", "")),
            metric_keys=tuple(d["metric_keys"]),
            baseline=_require_int(d["baseline"], "baseline"),
            baseline_event_seq=_require_int(d["baseline_event_seq"],
                                           "baseline_event_seq", 1),
            created_at=_require_int(d["created_at"], "created_at"),
            min_gap_days=_require_int(d.get("min_gap_days", DEFAULT_MIN_GAP_DAYS),
                                     "min_gap_days", 1),
            require_feedback=str(d.get("require_feedback", REQUIRED_FEEDBACK)),
            active=bool(d.get("active", True)),
        )


def _metric_keys_of(skill_id: str) -> tuple[str, ...]:
    rule = skill_rule_for(skill_id)
    if rule is None:
        raise ValueError(f"技能 {skill_id!r} 没有专属判定规则")
    return tuple(rule.required_keys)


def _main_metric(skill_id: str):
    rule = skill_rule_for(skill_id)
    return rule.metrics[0] if rule else None


class BenchmarkLedger:
    """基准卡与复测结果账本。独立 JSON 存档，不碰熟练度存档。"""

    def __init__(self, path: str | os.PathLike[str], clock=None) -> None:
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()
        self._cards: list[BenchmarkCard] = []
        self._results: list[dict] = []
        self._load()

    def _now(self) -> int:
        import time
        return int(self.clock() if self.clock else time.time())

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if int(raw.get("version", 0)) != SCHEMA:
            raise ValueError(f"基准账本版本不匹配: {raw.get('version')}")
        self._cards = [BenchmarkCard.from_dict(x)
                       for x in (raw.get("cards") or [])]
        self._results = [dict(x) for x in (raw.get("results") or [])]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA,
            "cards": [c.to_dict() for c in self._cards],
            "results": list(self._results),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    # ---------- 写 ----------
    def create(self, event: dict, *, now: int | None = None,
               min_gap_days: int = DEFAULT_MIN_GAP_DAYS) -> dict:
        """把一条真实有效练习登记为基准。"""
        if not isinstance(event, dict) or event.get("kind") != "practice" \
                or int(event.get("delta", 0) or 0) <= 0:
            raise ValueError("基准必须锚定一条真实的有效练习事件")
        skill_id = str(event["skill_id"])
        task_key = str(event.get("task_key") or "").strip()
        if not task_key:
            raise ValueError("该事件没有任务标识，不能设为基准")
        if event.get("feedback") != REQUIRED_FEEDBACK:
            raise ValueError("基准练习的反馈必须自报为「客观度量」")
        spec = _main_metric(skill_id)
        keys = _metric_keys_of(skill_id)
        metrics = event.get("metrics") or {}
        if set(metrics) != set(keys):
            raise ValueError("该事件的指标字段与现行技能规则不一致，不能设为基准")
        value = _require_int(metrics.get(spec.key), spec.label)
        t = int(now if now is not None else self._now())
        with self._lock:
            if any(c.active and c.skill_id == skill_id
                   and c.task_key == task_key for c in self._cards):
                raise ValueError("这个技能的同名任务已经是基准了")
            n = 1 + max((int(c.id[2:]) for c in self._cards
                         if c.id.startswith("b-") and c.id[2:].isdigit()),
                        default=0)
            card = BenchmarkCard(
                id=f"b-{n:04d}", skill_id=skill_id, task_key=task_key,
                metric_key=spec.key, metric_label=spec.label, unit=spec.unit,
                metric_keys=keys, baseline=value,
                baseline_event_seq=_require_int(event.get("seq"),
                                               "event.seq", 1),
                created_at=t,
                min_gap_days=_require_int(min_gap_days, "min_gap_days", 1),
            )
            self._cards.append(card)
            self._save()
            return card.to_dict()

    def observe(self, event: dict, *, now: int | None = None) -> dict | None:
        """每次有效练习后调用；命中基准口径就记一条复测结果。

        不满足口径时返回 None（不记账、不提示失败），避免把普通练习
        误记成复测。返回的结果里 verified 恒为 False。
        """
        if not isinstance(event, dict) or event.get("kind") != "practice" \
                or int(event.get("delta", 0) or 0) <= 0:
            return None
        t = int(now if now is not None else self._now())
        with self._lock:
            card = next(
                (c for c in self._cards
                 if c.active and c.skill_id == event.get("skill_id")
                 and c.task_key == event.get("task_key")), None)
            if card is None:
                return None
            seq = int(event.get("seq", 0))
            if seq == card.baseline_event_seq:
                return None
            if any(int(r.get("event_seq", 0)) == seq for r in self._results):
                return None  # 幂等：同一事件不重复记账
            if event.get("feedback") != card.require_feedback:
                return None
            metrics = event.get("metrics") or {}
            if set(metrics) != set(card.metric_keys):
                return None
            last_at = max(
                [card.created_at] +
                [int(r["at"]) for r in self._results
                 if r.get("card_id") == card.id])
            if t - last_at < card.min_gap_days * 86_400:
                return None
            actual = _require_int(metrics.get(card.metric_key),
                                  card.metric_label)
            result = {
                "card_id": card.id, "skill_id": card.skill_id,
                "task_key": card.task_key, "at": t, "event_seq": seq,
                "metric_key": card.metric_key, "metric_label": card.metric_label,
                "unit": card.unit, "baseline": card.baseline,
                "actual": actual, "delta": actual - card.baseline,
                "passed": actual > card.baseline,
                "reason": ("主指标高于基线" if actual > card.baseline
                           else "主指标未超过基线"),
                # 诚实标注：自报值，系统不校验真伪。
                "verified": False,
                "trusted": False,
            }
            self._results.append(result)
            self._save()
            return result

    def deactivate(self, card_id: str) -> dict:
        with self._lock:
            card = next((c for c in self._cards if c.id == str(card_id)), None)
            if card is None:
                raise KeyError(f"找不到基准卡 {card_id!r}")
            card.active = False
            self._save()
            return card.to_dict()

    def clear_all(self) -> int:
        with self._lock:
            n = len(self._cards) + len(self._results)
            self._cards, self._results = [], []
            self._save()
            return n

    # ---------- 读与自检 ----------
    def verify(self, event_seqs: set[int] | None = None) -> list[str]:
        problems = []
        ids = [c.id for c in self._cards]
        if len(ids) != len(set(ids)):
            problems.append("基准卡 id 重复")
        for c in self._cards:
            rule = skill_rule_for(c.skill_id)
            if rule is None or tuple(rule.required_keys) != c.metric_keys:
                problems.append(f"{c.id}: 口径锁与现行技能规则不一致")
            if event_seqs is not None and c.baseline_event_seq not in event_seqs:
                problems.append(f"{c.id}: 基准事件号不在真实日志中")
        for r in self._results:
            if event_seqs is not None and int(r.get("event_seq", 0)) \
                    not in event_seqs:
                problems.append(f"{r.get('card_id')}: 复测事件号不在真实日志中")
            if r.get("verified") or r.get("trusted"):
                problems.append("复测结果不得标记为已验真")
        return problems

    def pass_count(self) -> int:
        return sum(1 for r in self._results if r.get("passed"))

    def snapshot(self, *, now: int | None = None,
                 event_seqs: set[int] | None = None) -> dict:
        t = int(now if now is not None else self._now())
        with self._lock:
            cards = []
            for c in self._cards:
                row = c.to_dict()
                last = max([c.created_at] + [int(r["at"]) for r in self._results
                                             if r.get("card_id") == c.id])
                row["next_retest_at"] = last + c.min_gap_days * 86_400
                row["days_until_retest"] = max(
                    0, (row["next_retest_at"] - t + 86_399) // 86_400)
                row["results"] = [dict(r) for r in self._results
                                  if r.get("card_id") == c.id]
                if not c.active:
                    row["results"] = row["results"]
                cards.append(row)
            return {
                "ok": True,
                "cards": cards,
                "active_cards": [x for x in cards if x["active"]],
                "results": [dict(r) for r in self._results],
                "pass_count": self.pass_count(),
                "min_gap_days": DEFAULT_MIN_GAP_DAYS,
                "require_feedback": REQUIRED_FEEDBACK,
                "declaration": (
                    "复测只认「自报为客观度量」的同口径记录；本机不收图、不验真、"
                    "trusted 恒为 False。复测不写经验、不改保持度、不绕过任何判定门。"),
                "verify": self.verify(event_seqs),
                "save_path": str(self.path),
            }
