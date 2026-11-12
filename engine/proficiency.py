# -*- coding: utf-8 -*-
"""
熟练度系统 (Proficiency System) —— 可直接拷贝进现有项目的单文件模块。

v2：按现实生活技能成长规律建模。规则定义与判定标准集中在 realistic_rules.py，
本模块负责状态机、持久化与接口。原有接口全部保留。

设计要点
--------
1. 两层真值：total_exp 是历史累计有效经验（单调不减，学过不会忘光）；
   retention_bps 是保持度（0-10000 基点）。当前有效熟练度
   effective_exp = total_exp x retention_bps // 10000，等级由它派生。
   level 只是为查询/索引冗余的派生缓存，加载与结算时强制重算。

2. 阈值表：预计算「达到每一级所需的累计有效经验」，二分查找，全整数。
   默认 PowerRule（cost(L)=ceil(base x L^k), k>1）实现「提升先快后慢」。

3. 时间可注入：所有涉时操作接受显式 now（Unix 秒），或走构造时注入的
   clock。判定不隐式读系统时钟，因此完全可复现。生疏只在显式
   settle(now) / add_practice(now) / 带 now 的查询时结算。

4. 成长规则可序列化：幂律 / 线性 / 指数 / 命名自定义。规则随存档持久化。

5. 持久化抽象：Storage 协议 + JSON 原子写 / SQLite 事务 / 内存三种实现。
   存档带 schema_version；v1 自动迁移（新字段取默认值），更高版本拒绝读取。

6. 并发：RLock 保护全部状态；auto_save 可关闭并用 batch() 合并写盘。

Python 3.10+，仅依赖标准库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from pathlib import Path
from typing import Callable, Iterator, Protocol

try:  # 直接运行 engine/ 内脚本时是顶层模块；被 tests 以包形式导入时是相对导入
    from checkpoints import quiz_level
except ImportError:  # pragma: no cover
    from .checkpoints import quiz_level
from realistic_rules import (
    FEEDBACK_LEVELS, LEVEL_CRITERIA, MAX_LEVEL, REPEAT_WINDOW, SHAPE_RULES,
    STREAK_RULES, TRUST_RULES, PracticeContext, PracticeRecord, PracticeVerdict,
    advance_streak, assess_level, assess_skill_level, current_retention_bps,
    day_index, effective_exp, evaluate_practice, evaluate_skill_metrics,
    rebuild_streak, skill_rule_for, streak_bonus_bps, validate_tz_offset,
)
from realistic_rules import rulebook  # 有意 re-export：让调用方只 import 本模块也能取规则表

__all__ = [
    "SCHEMA_VERSION",
    "GrowthRule", "LinearRule", "ExponentialRule", "PowerRule", "NamedRule",
    "register_custom_rule", "SkillConfig", "SkillState", "SkillProgress",
    "LevelUpEvent", "LevelDownEvent", "AddExpResult", "PracticeResult",
    "PracticeEvent", "Snapshot", "Storage", "rulebook",
    "MemoryStorage", "JsonFileStorage", "SQLiteStorage", "ProficiencySystem",
]

SCHEMA_VERSION = 6  # v6: 存档记录时区偏移，修正按 UTC 日换日的错判
_MAX_TABLE_LEVELS = 100_000
_MAX_STATS_DAYS = 3_650      # 近 N 日统计的上限，避免构造超大序列
_TZ_MIGRATION_VERSION = 6   # v6 之前的存档按构造参数重建日历口径  # 防止 cost() 写错导致无限膨胀
_RUSTY_BPS = 9_000          # 保持度低于此值即标记为「生疏」
_EVENT_LOG_WARN = 20_000    # 事件日志超过此数量时 verify() 提示归档（不自动截断，避免破坏恒等式）
_PROOF_TYPES = tuple(TRUST_RULES["proof_type_values"])
_PROOF_URL_MAX = int(TRUST_RULES["proof_url_max_len"])


# --------------------------------------------------------------------------
# 基础校验
# --------------------------------------------------------------------------

def _unix_now() -> int:
    return int(time.time())


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(unix_seconds: int) -> str:
    """把显式 Unix 秒转成 ISO 时间字符串，仅用于展示，不参与任何判定。"""
    return datetime.fromtimestamp(unix_seconds, timezone.utc).isoformat(timespec="seconds")


def _require_int(value: object, name: str, minimum: int | None = None) -> int:
    # bool 是 int 的子类，必须显式排除，否则 True 会被当成 1 点经验
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是 int，实际为 {type(value).__name__}: {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 必须 >= {minimum}，实际为 {value}")
    return value


def _require_skill_id(skill_id: object) -> str:
    if not isinstance(skill_id, str) or not skill_id.strip():
        raise ValueError(f"skill_id 必须是非空字符串，实际为 {skill_id!r}")
    return skill_id


def _check_version(data: dict) -> None:
    version = _require_int(data.get("version", SCHEMA_VERSION), "version", 1)
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"存档版本 v{version} 高于本模块支持的 v{SCHEMA_VERSION}，"
            "请升级模块后再加载，拒绝静默读取以免算错等级"
        )
    # v1 -> v2：SkillState.from_dict 对新增字段全部提供默认值，
    # 旧存档按「刚完成一次有效练习」解释（保持度满、无历史练习统计）。
    # v2 -> v3：新增 append-only 事件日志与连胜状态，旧存档按「无历史事件、
    # 连胜为 0」解释；不从旧状态倒推事件，避免伪造历史。
    # v3 -> v4：事件新增 proof_type / proof_url（证明声明元数据）与 buff_bps
    # （悟道卡倍率，默认 10000 即 x1）。旧事件的三个字段由 from_dict 补默认值，
    # 不改写历史记录、不补造证明；连胜新增 freeze_charges 由 streak_info 补 0。
    # v4 -> v5：SkillState 新增 recent_sessions（近期窗口序列，用于截尾均值门槛）。
    # 旧存档该字段为空时，从 append-only 事件日志里回填真实练习记录，
    # 不倒推、不伪造；事件里本来就有 quality/difficulty/feedback/metrics。


def _level_name(level: int) -> str:
    row = next((c for c in LEVEL_CRITERIA if c["level"] == level), None)
    return row["name"] if row else f"Lv.{level}"


# --------------------------------------------------------------------------
# 成长规则：cost(L) = 从等级 L 升到 L+1 所需经验
# --------------------------------------------------------------------------

_CUSTOM_RULES: dict[str, Callable[[int], int]] = {}


def register_custom_rule(name: str, fn: Callable[[int], int]) -> None:
    """注册可持久化的自定义成长函数。

    自定义函数无法写进 JSON，所以按名字注册：存档只存名字，
    重启后必须再次调用本函数注册同名实现才能 load()。
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("自定义规则名必须是非空字符串")
    if not callable(fn):
        raise TypeError("fn 必须可调用")
    # 立刻试算一级，尽早暴露返回值类型错误
    _require_int(fn(1), f"自定义规则 {name!r} 第 1 级返回值", 1)
    _CUSTOM_RULES[name] = fn


class GrowthRule(ABC):
    """成长规则基类。子类必须实现 cost() 与 to_dict()。"""

    @abstractmethod
    def cost(self, level: int) -> int:
        """返回从 level 升到 level+1 所需经验，必须 >= 1。"""

    @abstractmethod
    def to_dict(self) -> dict:
        """序列化为可 JSON 化的字典。"""

    @staticmethod
    def from_dict(data: dict) -> "GrowthRule":
        if not isinstance(data, dict):
            raise TypeError(f"成长规则必须是对象，实际为 {type(data).__name__}")
        kind = data.get("type")
        if kind == "linear":
            return LinearRule(base=data["base"], step=data.get("step", 0))
        if kind == "exponential":
            return ExponentialRule(base=data["base"], factor=data.get("factor", "1.5"))
        if kind == "power":
            return PowerRule(base=data["base"], exponent=data.get("exponent", "1.5"))
        if kind == "named":
            return NamedRule(name=data["name"])
        raise ValueError(
            f"未知成长规则类型 {kind!r}，可选: power / linear / exponential / named"
        )


@dataclass(frozen=True)
class LinearRule(GrowthRule):
    """线性增长: cost(L) = base + step * (L - 1)。step=0 即每级固定经验。"""

    base: int = 100
    step: int = 0

    def __post_init__(self) -> None:
        _require_int(self.base, "base", 1)
        _require_int(self.step, "step", 0)

    def cost(self, level: int) -> int:
        _require_int(level, "level", 1)
        return self.base + self.step * (level - 1)

    def to_dict(self) -> dict:
        return {"type": "linear", "base": self.base, "step": self.step}


@dataclass(frozen=True)
class ExponentialRule(GrowthRule):
    """指数增长: cost(L) = ceil(base * factor ** (L - 1))。

    factor 用十进制字符串保存，并用 Decimal 计算，避免 1.1 这类
    二进制浮点在高等级放大成错误整数。
    """

    base: int = 100
    factor: str = "1.5"

    def __post_init__(self) -> None:
        _require_int(self.base, "base", 1)
        self._factor_decimal()  # 提前校验
        object.__setattr__(self, "factor", str(self.factor))  # 允许传 float，统一成字符串

    def _factor_decimal(self) -> Decimal:
        try:
            value = Decimal(str(self.factor))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"factor 必须是正数，实际为 {self.factor!r}") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError(f"factor 必须是有限正数，实际为 {self.factor!r}")
        return value

    def cost(self, level: int) -> int:
        _require_int(level, "level", 1)
        factor = self._factor_decimal()
        with localcontext() as ctx:
            ctx.prec = 80  # 足够覆盖任何现实中的等级区间
            needed = (Decimal(self.base) * (factor ** (level - 1))).to_integral_value(
                rounding=ROUND_CEILING
            )
        return max(1, int(needed))

    def to_dict(self) -> dict:
        return {"type": "exponential", "base": self.base, "factor": self.factor}


@dataclass(frozen=True)
class PowerRule(GrowthRule):
    """幂律增长（现实成长默认）：cost(L) = ceil(base * L^k)，k > 1。

    每级所需有效经验随等级按幂次上升，因此同样质量的练习在低等级
    连升数级、在高等级只前进一小截 —— 提升先快后慢。
    Decimal 计算，避免浮点在 k 为小数时产生不确定的进位。
    """

    base: int = 100
    exponent: str = "1.5"

    def __post_init__(self) -> None:
        _require_int(self.base, "base", 1)
        value = self._exponent_decimal()
        if value <= 1:
            raise ValueError(f"exponent 必须 > 1 才能体现先快后慢，实际为 {self.exponent!r}")
        object.__setattr__(self, "exponent", str(self.exponent))

    def _exponent_decimal(self) -> Decimal:
        try:
            value = Decimal(str(self.exponent))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"exponent 必须是正数，实际为 {self.exponent!r}") from exc
        if not value.is_finite():
            raise ValueError(f"exponent 必须是有限数，实际为 {self.exponent!r}")
        return value

    def cost(self, level: int) -> int:
        _require_int(level, "level", 1)
        k = self._exponent_decimal()
        with localcontext() as ctx:
            ctx.prec = 80
            needed = (Decimal(self.base) * (Decimal(level) ** k)).to_integral_value(
                rounding=ROUND_CEILING
            )
        return max(1, int(needed))

    def to_dict(self) -> dict:
        return {"type": "power", "base": self.base, "exponent": self.exponent}


@dataclass(frozen=True)
class NamedRule(GrowthRule):
    """自定义规则句柄：按名字查注册表，存档只存名字。"""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(f"自定义规则名必须是非空字符串，实际为 {self.name!r}")

    def cost(self, level: int) -> int:
        _require_int(level, "level", 1)
        try:
            fn = _CUSTOM_RULES[self.name]
        except KeyError:
            raise KeyError(
                f"自定义成长规则 {self.name!r} 未注册，加载存档前请先调用 "
                "register_custom_rule(name, fn)"
            ) from None
        return _require_int(fn(level), f"自定义规则 {self.name!r} 第 {level} 级返回值", 1)

    def to_dict(self) -> dict:
        return {"type": "named", "name": self.name}


@dataclass(frozen=True)
class SkillConfig:
    """单个技能的成长配置。max_level=None 表示不封顶。"""

    rule: GrowthRule = field(default_factory=PowerRule)
    max_level: int | None = MAX_LEVEL

    def __post_init__(self) -> None:
        if self.max_level is not None:
            _require_int(self.max_level, "max_level", 1)

    def to_dict(self) -> dict:
        return {"rule": self.rule.to_dict(), "max_level": self.max_level}

    @staticmethod
    def from_dict(data: dict) -> "SkillConfig":
        if not isinstance(data, dict):
            raise TypeError("SkillConfig 必须是对象")
        return SkillConfig(
            rule=GrowthRule.from_dict(data["rule"]),
            max_level=data.get("max_level", 100),
        )


# --------------------------------------------------------------------------
# 等级引擎：累计经验 -> (等级, 本级经验, 升级所需, 是否满级)
# --------------------------------------------------------------------------

class _LevelTable:
    """thresholds[k] = 达到等级 k+1 所需的累计经验；thresholds[0] = 0。"""

    def __init__(self, config: SkillConfig) -> None:
        self.config = config
        self._thresholds: list[int] = [0]

    def _append_next(self) -> None:
        level = len(self._thresholds)  # 当前最高等级
        cost = self.config.rule.cost(level)
        _require_int(cost, f"cost({level}) 返回值", 1)
        self._thresholds.append(self._thresholds[-1] + cost)
        if len(self._thresholds) > _MAX_TABLE_LEVELS:
            raise OverflowError("等级数超过安全上限，请检查成长规则或 max_level 配置")

    def _ensure(self, total_exp: int) -> None:
        # 循环条件用 <= 而不是 <：刚好达到阈值时也需要下一级阈值来算百分比
        while self._thresholds[-1] <= total_exp:
            if (self.config.max_level is not None
                    and len(self._thresholds) >= self.config.max_level):
                return
            self._append_next()

    def resolve(self, total_exp: int) -> tuple[int, int, int, bool]:
        """返回 (level, current_exp, required_exp, is_max_level)。"""
        _require_int(total_exp, "total_exp", 0)
        self._ensure(total_exp)
        idx = bisect_right(self._thresholds, total_exp) - 1
        level = idx + 1
        current_exp = total_exp - self._thresholds[idx]
        is_max = (self.config.max_level is not None
                  and level >= self.config.max_level)
        required = 0 if is_max else self._thresholds[idx + 1] - self._thresholds[idx]
        return level, current_exp, required, is_max

    def exp_for_level(self, target_level: int) -> int:
        """达到 target_level 所需的累计经验（用于 set_level）。"""
        _require_int(target_level, "target_level", 1)
        if self.config.max_level is not None and target_level > self.config.max_level:
            raise ValueError(f"目标等级 {target_level} 超过 max_level={self.config.max_level}")
        while len(self._thresholds) < target_level:
            self._append_next()
        return self._thresholds[target_level - 1]


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class SkillState:
    """持久化实体。

    total_exp（历史累计有效经验）与 retention_bps（保持度）是两层真值；
    level 是由 effective_exp 派生的缓存。v1 存档缺字段时全部取默认值，
    等价于「刚完成一次有效练习」，因此旧存档加载后等级不变。
    """

    skill_id: str
    total_exp: int = 0
    level: int = 1
    updated_at: str = field(default_factory=_utcnow)
    retention_bps: int = 10_000
    last_practice_at: int = 0
    valid_sessions: int = 0
    quality_sum: int = 0
    feedback_max: int = 0
    max_difficulty: int = 0
    low_streak: int = 0
    recent_fingerprints: list[str] = field(default_factory=list)
    settled_at: int = 0             # 最后一次显式结算的 Unix 秒
    metric_sums: dict[str, int] = field(default_factory=dict)  # 技能专属指标累计值
    # 指标的单次最好成绩（aggregate=max 的指标用它判等级，如跑步最长距离）
    metric_max: dict[str, int] = field(default_factory=dict)
    # v5：最近 N 次有效练习的窗口序列，用于「截尾均值」达标校验。
    # 终身累计值仍保留在 quality_sum / metric_sums，只作履历展示。
    recent_sessions: list[dict] = field(default_factory=list)
    # v7：首登自评得到的「信用等级地板」。它不增加 total_exp，只在
    # _granted_level 里兜住下限，避免新用户从 LV1 重新爬一遍已会的部分。
    # 0 = 没做过自评；设置后只在没有练习记录时允许写入。
    baseline_level: int = 0
    # 归档：从今日主线与推荐里隐藏，但保留全部记录（可随时恢复）。
    archived: bool = False
    # 等级验证（v8）：语言类技能 LV5 起要过一次客观小题，记下已验过的等级。
    verified_level: int = 0
    # 验证题累计答对数（满 3 题升级）；quiz_seen 记已出过的题，避免重复；
    # quiz_retry_after 记答错时的练习次数，必须再练一次才会重新出题。
    quiz_credit: int = 0
    quiz_seen: list[str] = field(default_factory=list)
    quiz_retry_after: int = 0

    def __post_init__(self) -> None:
        _require_skill_id(self.skill_id)
        _require_int(self.total_exp, "total_exp", 0)
        _require_int(self.level, "level", 1)
        _require_int(self.retention_bps, "retention_bps", 0)
        if not 0 <= self.retention_bps <= 10_000:
            raise ValueError("retention_bps 必须在 0..10000")
        _require_int(self.last_practice_at, "last_practice_at", 0)
        _require_int(self.valid_sessions, "valid_sessions", 0)
        _require_int(self.quality_sum, "quality_sum", 0)
        _require_int(self.feedback_max, "feedback_max", 0)
        _require_int(self.max_difficulty, "max_difficulty", 0)
        _require_int(self.low_streak, "low_streak", 0)
        _require_int(self.settled_at, "settled_at", 0)
        _require_int(self.baseline_level, "baseline_level", 0)
        if not isinstance(self.archived, bool):
            raise TypeError("archived 必须是 bool")
        _require_int(self.verified_level, "verified_level", 0)
        if self.verified_level and not 1 <= self.verified_level <= MAX_LEVEL:
            raise ValueError(f"verified_level 必须在 1..{MAX_LEVEL} 或 0")
        _require_int(self.quiz_credit, "quiz_credit", 0)
        _require_int(self.quiz_retry_after, "quiz_retry_after", 0)
        if not isinstance(self.quiz_seen, list) or not all(
            isinstance(x, str) for x in self.quiz_seen
        ):
            raise TypeError("quiz_seen 必须是字符串列表")
        if self.baseline_level and not 1 <= self.baseline_level <= MAX_LEVEL:
            raise ValueError(f"baseline_level 必须在 1..{MAX_LEVEL} 或 0（未自评）")
        if not isinstance(self.metric_sums, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            and v >= 0 for k, v in self.metric_sums.items()
        ):
            raise TypeError("metric_sums 必须是 {str: 非负整数}")
        if not isinstance(self.metric_max, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            and v >= 0 for k, v in self.metric_max.items()
        ):
            raise TypeError("metric_max 必须是 {str: 非负整数}")
        if not isinstance(self.recent_sessions, list) or not all(
            isinstance(row, dict) for row in self.recent_sessions
        ):
            raise TypeError("recent_sessions 必须是对象列表")

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "total_exp": self.total_exp,
            "level": self.level,
            "updated_at": self.updated_at,
            "retention_bps": self.retention_bps,
            "last_practice_at": self.last_practice_at,
            "valid_sessions": self.valid_sessions,
            "quality_sum": self.quality_sum,
            "feedback_max": self.feedback_max,
            "max_difficulty": self.max_difficulty,
            "low_streak": self.low_streak,
            "recent_fingerprints": list(self.recent_fingerprints),
            "settled_at": self.settled_at,
            "metric_sums": dict(self.metric_sums),
            "metric_max": dict(self.metric_max),
            "recent_sessions": [dict(row) for row in self.recent_sessions],
            "baseline_level": self.baseline_level,
            "archived": self.archived,
            "verified_level": self.verified_level,
            "quiz_credit": self.quiz_credit,
            "quiz_seen": list(self.quiz_seen),
            "quiz_retry_after": self.quiz_retry_after,
        }

    @staticmethod
    def from_dict(data: dict) -> "SkillState":
        if not isinstance(data, dict):
            raise TypeError("SkillState 必须是对象")
        updated_at = data.get("updated_at") or _utcnow()
        if not isinstance(updated_at, str):
            raise TypeError("updated_at 必须是字符串")
        prints = data.get("recent_fingerprints", [])
        if not isinstance(prints, list) or not all(isinstance(x, str) for x in prints):
            raise TypeError("recent_fingerprints 必须是字符串列表")
        raw_seen = data.get("quiz_seen") or []
        if not isinstance(raw_seen, list) or not all(isinstance(x, str) for x in raw_seen):
            raise TypeError("quiz_seen 必须是字符串列表")
        raw_max = data.get("metric_max") or {}
        if not isinstance(raw_max, dict):
            raise TypeError("metric_max 必须是对象")
        raw_sums = data.get("metric_sums") or {}
        if not isinstance(raw_sums, dict):
            raise TypeError("metric_sums 必须是对象")
        return SkillState(
            skill_id=_require_skill_id(data.get("skill_id")),
            total_exp=_require_int(data.get("total_exp", 0), "total_exp", 0),
            level=_require_int(data.get("level", 1), "level", 1),
            updated_at=updated_at,
            retention_bps=_require_int(data.get("retention_bps", 10_000),
                                       "retention_bps", 0),
            last_practice_at=_require_int(data.get("last_practice_at", 0),
                                          "last_practice_at", 0),
            valid_sessions=_require_int(data.get("valid_sessions", 0),
                                        "valid_sessions", 0),
            quality_sum=_require_int(data.get("quality_sum", 0), "quality_sum", 0),
            feedback_max=_require_int(data.get("feedback_max", 0), "feedback_max", 0),
            max_difficulty=_require_int(data.get("max_difficulty", 0),
                                        "max_difficulty", 0),
            low_streak=_require_int(data.get("low_streak", 0), "low_streak", 0),
            recent_fingerprints=list(prints),
            settled_at=_require_int(data.get("settled_at", 0), "settled_at", 0),
            metric_sums=dict(raw_sums),
            metric_max={str(k): int(v) for k, v in raw_max.items()},
            recent_sessions=[dict(row) for row in (data.get("recent_sessions") or [])
                             if isinstance(row, dict)],
            baseline_level=_require_int(data.get("baseline_level", 0),
                                        "baseline_level", 0),
            archived=bool(data.get("archived", False)),
            verified_level=_require_int(data.get("verified_level", 0),
                                       "verified_level", 0),
        )


@dataclass(frozen=True)
class SkillProgress:
    """查询视图：等级 / 本级经验 / 升级所需 / 百分比 / 保持度 / 生疏状态。"""

    skill_id: str
    level: int
    current_exp: int
    required_exp: int
    total_exp: int
    percent: float
    is_max_level: bool
    effective_exp: int = 0
    retention_bps: int = 10_000
    idle_days_bps: int = 0
    is_rusty: bool = False
    valid_sessions: int = 0
    level_name: str = ""
    last_practice_at: int = 0
    exp_level: int = 0                # 仅按有效熟练度解析出的等级（未过达标校验）
    gate_blocked: bool = False        # True = 经验已够升级，但达标校验未放行
    assess_gate_enabled: bool = False
    baseline_level: int = 0           # 首登自评的信用等级地板（0 = 未自评）
    archived: bool = False            # 归档后不进今日主线与推荐
    verified_level: int = 0           # 已通过等级验证的最高目标等级
    quiz_required: bool = False        # 下一级需要先过验证题
    quiz_credit: int = 0               # 已验证题累计答对数（满 3 升级）
    quiz_credits_needed: int = 3

    @property
    def remaining_exp(self) -> int:
        return max(0, self.required_exp - self.current_exp)

    @property
    def idle_days(self) -> float:
        return self.idle_days_bps / 10_000

    @property
    def retention_percent(self) -> float:
        return round(self.retention_bps / 100, 2)

    def bar(self, width: int = 20, filled: str = "#", empty: str = "-") -> str:
        """文本进度条，默认纯 ASCII，避免 Windows 控制台编码问题。"""
        width = _require_int(width, "width", 1)
        if self.is_max_level:
            return filled * width
        n = max(0, min(width, int(round(self.percent / 100.0 * width))))
        return filled * n + empty * (width - n)

    def display(self, width: int = 20) -> str:
        head = f"Lv.{self.level} {self.level_name}".strip()
        if self.is_max_level:
            return (f"{head} [MAX] | {self.bar(width)} 100.0% "
                    f"| 累计 {self.total_exp}")
        rust = " [生疏]" if self.is_rusty else ""
        gate = (f" [经验够 Lv.{self.exp_level}，达标校验未放行]"
                if self.gate_blocked else "")
        return (f"{head}{rust}{gate} | {self.current_exp}/{self.required_exp} "
                f"({self.percent:.1f}%) [{self.bar(width)}] "
                f"| 保持度 {self.retention_percent}% | 有效 {self.effective_exp}")

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "level": self.level,
            "level_name": self.level_name,
            "current_exp": self.current_exp,
            "required_exp": self.required_exp,
            "total_exp": self.total_exp,
            "effective_exp": self.effective_exp,
            "percent": self.percent,
            "remaining_exp": self.remaining_exp,
            "is_max_level": self.is_max_level,
            "retention_bps": self.retention_bps,
            "retention_percent": self.retention_percent,
            "idle_days": self.idle_days,
            "idle_days_bps": self.idle_days_bps,
            "is_rusty": self.is_rusty,
            "valid_sessions": self.valid_sessions,
            "last_practice_at": self.last_practice_at,
            "exp_level": self.exp_level,
            "gate_blocked": self.gate_blocked,
            "assess_gate_enabled": self.assess_gate_enabled,
            "baseline_level": self.baseline_level,
            "archived": self.archived,
            "verified_level": self.verified_level,
            "quiz_required": self.quiz_required,
            "quiz_credit": self.quiz_credit,
            "quiz_credits_needed": self.quiz_credits_needed,
        }


@dataclass(frozen=True)
class LevelUpEvent:
    skill_id: str
    from_level: int
    to_level: int
    total_exp: int
    reason: str
    at: str
    effective_exp: int = 0


@dataclass(frozen=True)
class LevelDownEvent:
    """生疏导致的等级退化。由显式 settle(now) 触发，不隐式发生。"""
    skill_id: str
    from_level: int
    to_level: int
    total_exp: int
    reason: str
    at: str
    effective_exp: int = 0
    retention_bps: int = 10_000


@dataclass(frozen=True)
class AddExpResult:
    before: SkillProgress
    after: SkillProgress
    delta: int                      # 累计经验的净变化，回档时为负数
    levels_gained: int              # 等级净变化，回档时为负数
    events: tuple[LevelUpEvent, ...]


@dataclass(frozen=True)
class PracticeResult:
    """一次练习的完整判定结果，含原因码与前后快照。"""
    verdict: PracticeVerdict
    before: SkillProgress
    after: SkillProgress
    levels_gained: int
    events: tuple


# --------------------------------------------------------------------------
# 持久化
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PracticeEvent:
    """一条不可变的判定 / 完成记录（append-only 事件日志）。

    参考文章的「底层只增事件记录」：每次熟练度变化都有据可查，可回溯、
    可核对、可防刷分。kind 取值：
      practice = 有效练习；rejected = 被判定拒绝的尝试；
      admin = 管理注入 / 回档；reset = 清空进度。
    delta 是该次对 total_exp 的净变化：practice > 0、rejected = 0，
    admin/reset 可为负（回档与重置是显式管理动作，不是隐式倒推）。
    """

    seq: int
    at: int
    skill_id: str
    kind: str
    delta: int = 0
    reason_code: str = ""
    task_key: str = ""
    difficulty: int = 0
    quality: int = 0
    feedback: str = ""
    streak_days: int = 0
    streak_bonus_bps: int = 0
    metrics: dict = field(default_factory=dict)
    # v4：验证层预留字段。proof_type / proof_url 只是【声明元数据】，
    # 本机不接收图片、不下载链接、不校验真伪、不做 AI 判定或身份绑定。
    proof_type: str = ""
    proof_url: str = ""
    # v5：练习随记。之前 add_practice(note=...) 是死字段（不进事件日志），
    # 现在写入同一条不可变事件，供「同一任务纵向时间线」展示。
    note: str = ""
    # v4：本次事件应用的增益倍率（基点）。10000 = 无效果（默认）；
    # 20000 = 悟道卡「下一次有效练习翻倍」。只由显式的 buff_bps 参数产生。
    buff_bps: int = 10_000
    # v7：练习后问卷的微调系数（8000–11200 基点）与原始答案。
    # 它只乘在最终增益上，不改变有效/无效判定，也不进入门槛统计。
    survey_bps: int = 10_000
    survey: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_int(self.seq, "seq", 0)
        _require_int(self.at, "at", 0)
        _require_skill_id(self.skill_id)
        if self.kind not in ("practice", "rejected", "admin", "reset"):
            raise ValueError(f"未知事件类型 {self.kind!r}")
        if isinstance(self.delta, bool) or not isinstance(self.delta, int):
            raise TypeError("delta 必须是 int")
        if self.proof_type not in _PROOF_TYPES:
            raise ValueError(f"未知 proof_type {self.proof_type!r}")
        if (not isinstance(self.proof_url, str)
                or len(self.proof_url) > _PROOF_URL_MAX):
            raise ValueError(f"proof_url 必须是长度 <= {_PROOF_URL_MAX} 的字符串")
        if self.buff_bps not in (10_000, 20_000):
            raise ValueError("buff_bps 只能是 10000 或 20000")
        if self.buff_bps != 10_000 and self.kind != "practice":
            raise ValueError("悟道卡倍率只能作用于有效练习事件")
        if isinstance(self.survey_bps, bool) or not isinstance(self.survey_bps, int):
            raise TypeError("survey_bps 必须是 int")
        if not 8_000 <= self.survey_bps <= 11_200:
            raise ValueError("survey_bps 必须在 8000..11200 基点")
        if not isinstance(self.survey, dict):
            raise TypeError("survey 必须是对象")

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "at": self.at, "skill_id": self.skill_id,
            "kind": self.kind, "delta": self.delta,
            "reason_code": self.reason_code, "task_key": self.task_key,
            "difficulty": self.difficulty, "quality": self.quality,
            "feedback": self.feedback, "streak_days": self.streak_days,
            "streak_bonus_bps": self.streak_bonus_bps,
            "metrics": dict(self.metrics),
            "proof_type": self.proof_type, "proof_url": self.proof_url,
            "note": self.note, "buff_bps": self.buff_bps,
            "survey_bps": self.survey_bps, "survey": dict(self.survey),
        }

    @staticmethod
    def from_dict(data: dict) -> "PracticeEvent":
        if not isinstance(data, dict):
            raise TypeError("PracticeEvent 必须是对象")
        metrics = data.get("metrics") or {}
        if not isinstance(metrics, dict):
            raise TypeError("PracticeEvent.metrics 必须是对象")
        return PracticeEvent(
            seq=_require_int(data.get("seq", 0), "seq", 0),
            at=_require_int(data.get("at", 0), "at", 0),
            skill_id=_require_skill_id(data.get("skill_id")),
            kind=str(data.get("kind", "")),
            delta=_require_int(data.get("delta", 0), "delta"),
            reason_code=str(data.get("reason_code", "")),
            task_key=str(data.get("task_key", "")),
            difficulty=_require_int(data.get("difficulty", 0), "difficulty"),
            quality=_require_int(data.get("quality", 0), "quality"),
            feedback=str(data.get("feedback", "")),
            streak_days=_require_int(data.get("streak_days", 0), "streak_days", 0),
            streak_bonus_bps=_require_int(
                data.get("streak_bonus_bps", 0), "streak_bonus_bps", 0
            ),
            metrics=dict(metrics),
            proof_type=str(data.get("proof_type", "")),
            proof_url=str(data.get("proof_url", "")),
            note=str(data.get("note", "")),
            buff_bps=_require_int(data.get("buff_bps", 10_000), "buff_bps", 0),
            survey_bps=_require_int(data.get("survey_bps", 10_000),
                                    "survey_bps", 0),
            survey=dict(survey) if isinstance(survey := data.get("survey"), dict) else {},
        )


@dataclass
class Snapshot:
    """一次完整存档：版本 + 配置 + 状态 + append-only 事件日志 + 连胜状态。"""

    version: int = SCHEMA_VERSION
    configs: dict[str, SkillConfig] = field(default_factory=dict)
    states: dict[str, SkillState] = field(default_factory=dict)
    events: tuple[PracticeEvent, ...] = ()
    streak: dict = field(default_factory=dict)
    tz_offset_seconds: int = 0       # v6：日历口径；0 = 旧版 UTC
    migrated_from: int | None = None  # 仅内存标记，不写进存档

    def __post_init__(self) -> None:
        _require_int(self.version, "version", 1)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "configs": {k: v.to_dict() for k, v in self.configs.items()},
            "states": {k: v.to_dict() for k, v in self.states.items()},
            "events": [e.to_dict() for e in self.events],
            "streak": dict(self.streak),
            "tz_offset_seconds": validate_tz_offset(self.tz_offset_seconds),
        }

    @staticmethod
    def from_dict(data: dict) -> "Snapshot":
        if not isinstance(data, dict):
            raise TypeError(f"存档根节点必须是对象，实际为 {type(data).__name__}")
        _check_version(data)
        source_version = _require_int(data.get("version", SCHEMA_VERSION), "version", 1)
        raw_configs = data.get("configs") or {}
        raw_states = data.get("states") or {}
        if not isinstance(raw_configs, dict) or not isinstance(raw_states, dict):
            raise TypeError("configs / states 必须是对象")
        configs: dict[str, SkillConfig] = {}
        states: dict[str, SkillState] = {}
        for sid, raw in raw_configs.items():
            configs[_require_skill_id(sid)] = SkillConfig.from_dict(raw)
        for sid, raw in raw_states.items():
            _require_skill_id(sid)
            if not isinstance(raw, dict):
                raise TypeError(f"技能 {sid!r} 的状态必须是对象")
            states[sid] = SkillState.from_dict({**raw, "skill_id": sid})
        raw_events = data.get("events") or []
        if not isinstance(raw_events, list):
            raise TypeError("events 必须是数组")
        events = tuple(PracticeEvent.from_dict(e) for e in raw_events)
        raw_streak = data.get("streak") or {}
        if not isinstance(raw_streak, dict):
            raise TypeError("streak 必须是对象")
        return Snapshot(
            version=SCHEMA_VERSION, configs=configs, states=states,
            events=events, streak=dict(raw_streak),
            tz_offset_seconds=validate_tz_offset(
                data.get("tz_offset_seconds", 0)),
            migrated_from=source_version if source_version < SCHEMA_VERSION else None,
        )


class Storage(Protocol):
    """持久化接口。换存储只需实现 load / save 两个方法。"""

    def load(self) -> Snapshot: ...

    def save(self, snapshot: Snapshot) -> None: ...


class MemoryStorage:
    """进程内存储，用于测试与无磁盘环境。走一遍序列化以验证数据可 JSON 化。"""

    def __init__(self) -> None:
        self._raw: dict = Snapshot().to_dict()

    def load(self) -> Snapshot:
        return Snapshot.from_dict(self._raw)

    def save(self, snapshot: Snapshot) -> None:
        self._raw = snapshot.to_dict()


class JsonFileStorage:
    """JSON 文件存储，原子写入：同目录临时文件 + fsync + os.replace。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> Snapshot:
        if not self.path.exists():
            return Snapshot()
        text = self.path.read_text(encoding="utf-8")
        if not text.strip():
            return Snapshot()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"存档 JSON 损坏: {self.path} ({exc})") from exc
        return Snapshot.from_dict(data)

    def save(self, snapshot: Snapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".proficiency-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snapshot.to_dict(), fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


_DDL = """
CREATE TABLE IF NOT EXISTS proficiency_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proficiency_config (
    skill_id TEXT PRIMARY KEY,
    payload  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proficiency_state (
    skill_id   TEXT PRIMARY KEY,
    total_exp  INTEGER NOT NULL,
    level      INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    payload    TEXT
);
"""


def _ensure_state_columns(conn: sqlite3.Connection) -> None:
    """v1 数据库没有 payload 列，就地补列，旧行由 load() 迁移。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(proficiency_state)")}
    if "payload" not in columns:
        conn.execute("ALTER TABLE proficiency_state ADD COLUMN payload TEXT")
        conn.commit()


def _delete_stale(conn: sqlite3.Connection, table: str, keep: list[str]) -> None:
    # table 只来自下面的两个硬编码常量，不存在注入面
    if keep:
        marks = ",".join("?" for _ in keep)
        conn.execute(f"DELETE FROM {table} WHERE skill_id NOT IN ({marks})", tuple(keep))
    else:
        conn.execute(f"DELETE FROM {table}")


class SQLiteStorage:
    """SQLite 存储，WAL + 单事务 upsert，适合多进程 / 需要 SQL 查询的场景。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def load(self) -> Snapshot:
        conn = self._connect()
        try:
            conn.executescript(_DDL)
            _ensure_state_columns(conn)
            meta = dict(conn.execute(
                "SELECT key, value FROM proficiency_meta"
            ).fetchall())
            if "version" in meta:
                _check_version({"version": int(meta["version"])})
            configs = {
                sid: SkillConfig.from_dict(json.loads(payload))
                for sid, payload in conn.execute(
                    "SELECT skill_id, payload FROM proficiency_config"
                )
            }
            states = {}
            had_legacy = False
            for sid, payload, total, level, updated_at in conn.execute(
                "SELECT skill_id, payload, total_exp, level, updated_at "
                "FROM proficiency_state"
            ):
                if payload:
                    states[sid] = SkillState.from_dict(json.loads(payload))
                else:  # v1 行：缺字段由 from_dict 默认值补齐
                    had_legacy = True
                    states[sid] = SkillState.from_dict({
                        "skill_id": sid, "total_exp": total,
                        "level": level, "updated_at": updated_at,
                    })
            old_version = int(meta.get("version", SCHEMA_VERSION))
            migrated = had_legacy or old_version < SCHEMA_VERSION
            events = tuple(PracticeEvent.from_dict(e) for e in
                           json.loads(meta.get("events", "[]")))
            streak = json.loads(meta.get("streak", "{}"))
            return Snapshot(
                version=SCHEMA_VERSION, configs=configs, states=states,
                events=events, streak=streak,
                tz_offset_seconds=validate_tz_offset(
                    int(meta.get("tz_offset_seconds", 0))),
                migrated_from=old_version if migrated else None,
            )
        finally:
            conn.close()

    def save(self, snapshot: Snapshot) -> None:
        conn = self._connect()
        try:
            conn.executescript(_DDL)
            _ensure_state_columns(conn)
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO proficiency_meta(key, value) VALUES(?, ?)",
                    [
                        ("version", str(SCHEMA_VERSION)),
                        ("events", json.dumps([e.to_dict() for e in snapshot.events],
                                              ensure_ascii=False)),
                        ("streak", json.dumps(snapshot.streak, ensure_ascii=False)),
                        ("tz_offset_seconds", str(validate_tz_offset(
                            snapshot.tz_offset_seconds))),
                    ],
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO proficiency_config(skill_id, payload) VALUES(?, ?)",
                    [(sid, json.dumps(cfg.to_dict(), ensure_ascii=False))
                     for sid, cfg in snapshot.configs.items()],
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO proficiency_state"
                    "(skill_id, total_exp, level, updated_at, payload) VALUES(?, ?, ?, ?, ?)",
                    [(sid, st.total_exp, st.level, st.updated_at,
                      json.dumps(st.to_dict(), ensure_ascii=False))
                     for sid, st in snapshot.states.items()],
                )
                _delete_stale(conn, "proficiency_config", list(snapshot.configs))
                _delete_stale(conn, "proficiency_state", list(snapshot.states))
        finally:
            conn.close()


# --------------------------------------------------------------------------
# 系统主体
# --------------------------------------------------------------------------

class ProficiencySystem:
    """熟练度系统门面。

    - add_practice(): 主写入口，走完整有效练习判定（难度/质量/反馈/重复）
    - add_exp() / set_total_exp() / set_level(): 兼容旧接口，直接注入有效经验
    - settle(): 显式按时间结算生疏与等级退化
    - get_progress() / list_progress() / assess(): 查询与可观测达标判定
    - on_level_up() / on_level_down(): 订阅升级与退化事件
    - flush() / batch(): 持久化控制
    - verify(): 内存状态与存档一致性自检

    clock 可注入（返回 Unix 秒整数），所有涉时方法也可显式传 now，
    因此同一组输入永远得到同一组结果，判定可复现。

    strict=False 时，首次 add_exp 会自动按 default_config 创建技能
    （对应小说里「首次使用即领悟技能」）；strict=True 则要求先 register_skill。
    """

    def __init__(
        self,
        storage: Storage,
        *,
        default_config: SkillConfig | None = None,
        auto_save: bool = True,
        strict: bool = False,
        clock: Callable[[], int] | None = None,
        assess_gate: bool = True,
        shape_enabled: bool | None = None,
        tz_offset_seconds: int = 0,
        extra_rules: dict | None = None,
    ) -> None:
        # 日历口径（同日递减 / 连胜 / 按日统计）统一由这里注入。
        # 0 = 历史 UTC 口径，保持纯函数与旧存档逐位兼容。
        self._tz_offset = validate_tz_offset(tz_offset_seconds)
        self._storage = storage
        self._default_config = default_config or SkillConfig()
        self._auto_save = bool(auto_save)
        self._strict = bool(strict)
        self._clock = clock or _unix_now
        # 拍板结果 assess_as_gate=hard_gate：升级前必须逐项通过可观测达标校验。
        # 设为 False 可退回「只看经验」的旧行为（等级引擎单测用此模式）。
        self._assess_gate = bool(assess_gate)
        # 2026-11-08 诊断修正：近期窗口门槛 + 钟形难度 / 同日递减 / 间隔加成。
        # 显式传 False 可完整回退到修正前口径（对照仿真与引擎契约测试用）。
        self._shape_enabled = (bool(SHAPE_RULES["enabled"])
                               if shape_enabled is None else bool(shape_enabled))
        self._lock = threading.RLock()
        # 用户自定义技能的规则表按实例注入（多用户之间互不共享）。
        self._extra_rules: dict = dict(extra_rules or {})
        self._configs: dict[str, SkillConfig] = {}
        self._tables: dict[str, _LevelTable] = {}
        self._states: dict[str, SkillState] = {}
        self._listeners: list[Callable[[LevelUpEvent], None]] = []
        self._down_listeners: list[Callable[[LevelDownEvent], None]] = []
        self._batch_depth = 0
        self._dirty = False
        # append-only 事件日志与全局连胜状态（参考产品文章的「底层事件记录」+「连胜维度」）
        self._events: list[PracticeEvent] = []
        self._streak: dict = {}
        self._load()

    def _now(self, explicit: int | None = None) -> int:
        value = explicit if explicit is not None else self._clock()
        return _require_int(value, "now", 0)

    # ---------- 内部 ----------

    def _load(self) -> None:
        snapshot = self._storage.load()
        # 自愈：剔除历史版本可能写入的零增量 admin/reset 事件并重新编号，
        # 它们不贡献经验，只会淹没真实记录；seq 必须保持从 1 连续。
        kept = [e for e in snapshot.events
                if not (e.kind in ("admin", "reset") and e.delta == 0)]
        events_corrected = len(kept) != len(snapshot.events)
        self._events = [replace(e, seq=i + 1) for i, e in enumerate(kept)]
        self._streak = dict(snapshot.streak)
        now = self._now()
        corrected = False
        # 存档记录的日历口径与本次注入不一致（含 v5 之前没记录口径的存档）：
        # 不混用两种口径，统一按注入值重建连胜。只重建「天序号」派生的状态，
        # total_exp / 事件日志 / 保持度一个字节都不改。
        if snapshot.tz_offset_seconds != self._tz_offset:
            self._streak = rebuild_streak(
                self._events, tz_offset_seconds=self._tz_offset, now=now,
                freeze_charges=self._streak.get("freeze_charges", 0))
            self._streak["timezone_migrated_from"] = snapshot.tz_offset_seconds
            corrected = True  # 把 v6 时区口径写回存档
        for sid, cfg in snapshot.configs.items():
            self._configs[sid] = cfg
            self._tables[sid] = _LevelTable(cfg)
        for sid, state in snapshot.states.items():
            if sid not in self._configs:
                self._configs[sid] = self._default_config
                self._tables[sid] = _LevelTable(self._default_config)
            # 精确恢复到「存档记录的最后一次结算时刻」；生疏不隐式发生，
            # 当前时刻的衰减留给后续显式 settle(now) / 带 now 的查询。
            at = state.settled_at or now  # v1 无该字段，按当前时钟解释
            state.settled_at = at
            # 规则变更后清掉已不属于当前技能的指标累计值，避免旧字段误导判定
            rule = skill_rule_for(sid, self._extra_rules)
            if rule is not None:
                valid_keys = {m.key for m in rule.metrics}
                stale = [k for k in state.metric_sums if k not in valid_keys]
                if stale:
                    for k in stale:
                        state.metric_sums.pop(k, None)
                        state.metric_max.pop(k, None)
                    corrected = True
            # v4 -> v5：窗口为空时从 append-only 事件日志回填真实练习，
            # 不倒推、不伪造；事件里本来就有 quality/difficulty/feedback/metrics。
            if not state.recent_sessions and self._events:
                for ev in self._events:
                    if ev.skill_id == sid and ev.kind == "practice":
                        state.recent_sessions.append({
                            "at": ev.at,
                            "quality": ev.quality,
                            "difficulty": ev.difficulty,
                            "feedback": FEEDBACK_LEVELS.get(ev.feedback, 0),
                            "metrics": dict(ev.metrics),
                        })
                state.recent_sessions = state.recent_sessions[
                    -int(SHAPE_RULES["window"]):]
                if state.recent_sessions:
                    corrected = True
            derived = self._derived_level(sid, state, at)
            if state.level != derived:
                state.level = derived
                corrected = True
            self._states[sid] = state
        if snapshot.migrated_from is not None and not self._events:
            # 旧存档没有事件日志：只写一条显式标注的「迁移基线」，
            # 不伪造历史练习，但让 sum(delta) == total_exp 恒等式成立。
            for sid, state in self._states.items():
                if state.total_exp:
                    self._record_event(
                        skill_id=sid, at=state.settled_at or now, kind="admin",
                        delta=state.total_exp, reason_code="migrated_baseline",
                    )
            corrected = True
        if (corrected or events_corrected or snapshot.migrated_from is not None) \
                and self._auto_save:
            self._mark_dirty()  # 自愈结果 / 迁移后的 v3 格式必须写回存档

    def _retention(self, state: SkillState, now: int) -> int:
        return current_retention_bps(state.retention_bps, state.last_practice_at, now)

    def _effective(self, state: SkillState, now: int) -> int:
        return effective_exp(state.total_exp, self._retention(state, now))

    def _stats_unlocked(self, state: SkillState, now: int) -> dict:
        """可观测达标校验所需的统计快照；调用方需持有锁。"""
        return {
            "valid_sessions": state.valid_sessions,
            "quality_sum": state.quality_sum,
            "feedback_max": state.feedback_max,
            "max_difficulty": state.max_difficulty,
            "metric_max": dict(state.metric_max),
            "idle_days_bps": self._idle_bps(state, now),
            # shape_enabled=False 时不下传窗口，规则层自动回退终身累计口径，
            # 保证「回退」是完整的（增益与门槛同时回到修正前）。
            "recent_sessions": ([dict(row) for row in state.recent_sessions]
                                if self._shape_enabled else []),
        }

    def _baseline_floor(self, state: SkillState) -> int:
        """自评信用的等级地板；0 表示没做过自评。"""
        raw = int(getattr(state, "baseline_level", 0) or 0)
        return max(0, min(MAX_LEVEL, raw))

    def _granted_level(self, skill_id: str, state: SkillState, now: int) -> int:
        """经验等级经「可观测达标校验」放行后的实际等级。

        拍板选项 assess_as_gate=hard_gate：经验只决定够到哪一级，真正放行
        要逐级通过 assess_skill_level。门槛只约束升级方向；已获等级不会被
        达标校验二次剥夺，生疏退化也照常按保持度发生。
        管理注入接口先把 state.level 设到经验等级，等于管理员直接授予等级。

        v7 自评起点：baseline_level 是「已会的信用」，不写 total_exp，
        因此它在两处生效——经验没到时兜住下限，经验到了从地板之上继续
        逐级校验。它最高只到 BASELINE_MAX_LEVEL（由服务层限制）。
        """
        exp_level = self._tables[skill_id].resolve(self._effective(state, now))[0]
        floor = min(self._tables[skill_id].config.max_level,
                    self._baseline_floor(state))
        if not self._assess_gate:
            return max(floor, exp_level)
        # 从「已授予等级」与「自评地板」中较高者起算，避免自评后被门槛打回。
        start = max(state.level, floor)
        if exp_level <= start:
            return max(floor, exp_level)
        rule = skill_rule_for(skill_id, self._extra_rules)
        if rule is None:
            return max(floor, exp_level)  # 未定义专属规则的技能走通用兜底
        stats = self._stats_unlocked(state, now)
        granted = start
        need = quiz_level(skill_id)
        for target in range(start + 1, exp_level + 1):
            if not assess_skill_level(rule, target, stats, state.metric_sums)["qualified"]:
                break
            # 语言类技能 LV5 起要过一次客观小题：指标达标只说明「够格考」，
            # 做题通过才算真到这一级。
            if need and target >= need and state.verified_level < target:
                break
            granted = target
        return max(floor, granted)

    def _derived_level(self, skill_id: str, state: SkillState, now: int) -> int:
        return self._granted_level(skill_id, state, now)

    def _idle_bps(self, state: SkillState, now: int) -> int:
        if state.last_practice_at <= 0:
            return 0
        return max(0, now - state.last_practice_at) * 10_000 // 86_400

    def _ensure_skill(self, skill_id: str) -> bool:
        """确保技能存在，返回 True 表示本次是新建（调用方需负责落盘）。"""
        if skill_id in self._states:
            return False
        if self._strict:
            raise KeyError(f"技能 {skill_id!r} 未注册（strict=True），请先 register_skill()")
        self._configs[skill_id] = self._default_config
        self._tables[skill_id] = _LevelTable(self._default_config)
        self._states[skill_id] = SkillState(skill_id=skill_id, total_exp=0, level=1)
        return True

    def _progress(self, skill_id: str, now: int | None = None) -> SkillProgress:
        t = self._now(now)
        state = self._states[skill_id]
        table = self._tables[skill_id]
        retention = self._retention(state, t)
        eff = effective_exp(state.total_exp, retention)
        exp_level, current, required, is_max = table.resolve(eff)
        level = self._granted_level(skill_id, state, t)
        gate_blocked = level < exp_level
        if gate_blocked:
            # 经验已越过本级上限，只因达标校验未放行而卡级。
            # 本级进度显示 100%，并单独给出 exp_level 说明「经验够到哪」。
            threshold = table.exp_for_level(level)
            current = max(0, eff - threshold)
            required = table.config.rule.cost(level)
            is_max = False
            percent = 100.0
        elif level > exp_level:
            # 自评起点高于经验等级：这是「信用下限」，不是练出来的。
            # 按当前等级算进度，让用户看到「要把经验补到这一级还差多少」。
            threshold = table.exp_for_level(level)
            current = max(0, eff - threshold)
            required = (0 if is_max
                        else table.config.rule.cost(level))
            percent = 0.0 if required == 0 else round(
                min(100.0, max(0.0, current / required * 100.0)), 4)
        elif is_max:
            percent = 100.0
        elif required == 0:
            percent = 0.0
        else:
            percent = round(min(100.0, max(0.0, current / required * 100.0)), 4)
        return SkillProgress(
            skill_id=skill_id,
            level=level,
            current_exp=current,
            required_exp=required,
            total_exp=state.total_exp,
            percent=percent,
            is_max_level=is_max,
            effective_exp=eff,
            retention_bps=retention,
            idle_days_bps=self._idle_bps(state, t),
            is_rusty=retention < _RUSTY_BPS,
            valid_sessions=state.valid_sessions,
            level_name=_level_name(level),
            last_practice_at=state.last_practice_at,
            exp_level=exp_level,
            gate_blocked=gate_blocked,
            assess_gate_enabled=self._assess_gate,
            baseline_level=self._baseline_floor(state),
            archived=bool(state.archived),
            verified_level=int(state.verified_level),
            quiz_credit=int(state.quiz_credit),
            quiz_credits_needed=3,
            quiz_required=bool(
                quiz_level(skill_id)
                and level + 1 >= quiz_level(skill_id)
                and int(state.verified_level) < level + 1
                and exp_level >= level + 1
                and int(state.valid_sessions) > int(state.quiz_retry_after)
            ),
        )

    def _snapshot(self) -> Snapshot:
        return Snapshot(
            version=SCHEMA_VERSION,
            configs=dict(self._configs),
            states={sid: SkillState(**st.to_dict()) for sid, st in self._states.items()},
            events=tuple(self._events),
            streak=dict(self._streak),
            tz_offset_seconds=self._tz_offset,
        )

    def _record_event(self, *, skill_id: str, at: int, kind: str, delta: int,
                      reason_code: str = "", task_key: str = "",
                      difficulty: int = 0, quality: int = 0, feedback: str = "",
                      metrics: dict | None = None,
                      proof_type: str = "", proof_url: str = "",
                      note: str = "", buff_bps: int = 10_000,
                      survey_bps: int = 10_000, survey: dict | None = None,
                      ) -> PracticeEvent:
        """追加一条不可变事件。调用方必须持有锁；不单独落盘，由调用方统一写。"""
        event = PracticeEvent(
            seq=(self._events[-1].seq + 1) if self._events else 1,
            at=at, skill_id=skill_id, kind=kind, delta=delta,
            reason_code=reason_code, task_key=task_key,
            difficulty=difficulty, quality=quality, feedback=feedback,
            streak_days=int(self._streak.get("current_days", 0)),
            streak_bonus_bps=streak_bonus_bps(
                int(self._streak.get("current_days", 0))
            ),
            metrics=dict(metrics or {}),
            proof_type=proof_type, proof_url=proof_url,
            note=str(note)[:500], buff_bps=buff_bps,
            survey_bps=survey_bps, survey=dict(survey or {}),
        )
        self._events.append(event)
        self._dirty = True
        return event

    def _mark_dirty(self) -> None:
        self._dirty = True
        if self._auto_save and self._batch_depth == 0:
            self._flush()

    def _flush(self) -> None:
        if not self._dirty:
            return
        self._storage.save(self._snapshot())
        self._dirty = False

    @staticmethod
    def _emit(events: tuple, listeners: list) -> None:
        """在锁外派发事件，避免回调阻塞其他线程或造成锁顺序问题。"""
        for event in events:
            for callback in listeners:
                callback(event)

    @staticmethod
    def _dispatch(events: tuple, ups: list, downs: list) -> None:
        """按事件实际发生顺序派发。

        不能按「升级组 / 退化组」分组派发：同一次练习可能先生疏降级、
        再因练习升级，分组会把顺序颠倒成「先升级后退化」。
        """
        for event in events:
            for callback in (ups if isinstance(event, LevelUpEvent) else downs):
                callback(event)

    def _apply(self, skill_id: str, new_total: int, reason: str,
               now: int | None = None) -> tuple[
        SkillProgress, SkillProgress, tuple[LevelUpEvent, ...]
    ]:
        """兼容旧接口的直接注入：视为「刚完成一次有效练习」。

        保持度回满、上次练习时间设为 now，因此有效熟练度 = 累计经验。
        """
        t = self._now(now)
        self._ensure_skill(skill_id)
        before = self._progress(skill_id, t)
        state = self._states[skill_id]
        old_total = state.total_exp
        state.total_exp = _require_int(new_total, "total_exp", 0)
        state.retention_bps = 10_000
        state.last_practice_at = t
        state.updated_at = _iso(t)
        state.settled_at = t
        # 管理注入 / 回档：直接授予经验对应等级，不走练习路径的达标校验。
        # 这是显式的管理员操作（数值重平衡、导入旧存档、演示快进），
        # 不是业务层的「练习升级」，因此不受 hard_gate 约束。
        state.level = self._tables[skill_id].resolve(self._effective(state, t))[0]
        after = self._progress(skill_id, t)
        state.level = after.level
        events: tuple[LevelUpEvent, ...] = ()
        if after.level > before.level:
            events = (LevelUpEvent(
                skill_id=skill_id,
                from_level=before.level,
                to_level=after.level,
                total_exp=state.total_exp,
                reason=reason,
                at=state.updated_at,
                effective_exp=after.effective_exp,
            ),)
        # 管理注入 / 回档同样入事件日志，保证「按技能 sum(delta) == total_exp」恒等式。
        # 零变化不记，避免无增量事件淹没真实记录。
        admin_delta = state.total_exp - old_total
        if admin_delta:
            self._record_event(
                skill_id=skill_id, at=t, kind="admin", delta=admin_delta,
                reason_code="admin_injection", task_key=reason or "_apply",
            )
        self._mark_dirty()
        return before, after, events

    # ---------- 现实成长：时间结算、有效练习、达标校验 ----------

    def _settle_unlocked(self, skill_id: str, now: int) -> list:
        """显式时间结算：按保持度重算等级，生成升级 / 退化事件。

        调用方必须持有锁。只置 _dirty，不落盘；由调用方统一 _mark_dirty()。
        """
        state = self._states[skill_id]
        retention = self._retention(state, now)
        derived = self._derived_level(skill_id, state, now)
        if derived == state.level:
            return []
        old = state.level
        # 1 天退化宽限期由 retention_cap_bps 实现（1 天内保持度恒为 10000），
        # 因此这里派生的等级与 _progress 的查询视图天然一致。
        state.level = derived
        state.settled_at = now
        state.updated_at = _iso(now)
        self._dirty = True
        common = dict(
            skill_id=skill_id, from_level=old, to_level=derived,
            total_exp=state.total_exp, reason="时间结算（生疏 / 恢复）",
            at=state.updated_at, effective_exp=self._effective(state, now),
        )
        if derived > old:
            return [LevelUpEvent(**common)]
        return [LevelDownEvent(**common, retention_bps=retention)]

    def settle(self, skill_id: str, *, now: int | None = None) -> SkillProgress:
        """按给定时刻结算生疏与等级退化。不隐式发生，必须由调用方触发。"""
        skill_id = _require_skill_id(skill_id)
        with self._lock:
            t = self._now(now)
            self._ensure_skill(skill_id)
            events = tuple(self._settle_unlocked(skill_id, t))
            if events:
                self._mark_dirty()
            progress = self._progress(skill_id, t)
            ups, downs = list(self._listeners), list(self._down_listeners)
        self._dispatch(events, ups, downs)
        return progress

    def add_practice(
        self,
        skill_id: str,
        *,
        difficulty: int,
        quality: int,
        feedback: str,
        task_key: str,
        now: int | None = None,
        note: str = "",
        metrics: dict | None = None,
        proof_type: str = "",
        proof_url: str = "",
        buff_bps: int = 10_000,
        survey_bps: int = 10_000,
        survey: dict | None = None,
    ) -> PracticeResult:
        """记录一次练习。

        v4 新增三个可选参数：
          proof_type / proof_url：验证层【声明元数据】。本机不接收图片、
            不下载链接、不校验真伪；只是把声明写进事件，供将来补验。
          buff_bps：本次增益倍率，仅允许 10000（默认，x1）或 20000（x2）。
            由利他层的「悟道卡」显式使用产生；默认不改变任何判定结果。

        判定分两层，都是纯函数、顺序固定：
          1 技能专属门（evaluate_skill_metrics）：指标完整性/范围/疼痛/有效门槛，
            决定「这次熟练度能不能增加」以及技能加成系数 S。
          2 通用门（evaluate_practice）：难度/质量/反馈/重复/时间，
            决定基础增益。最终 gain = round(基础增益 x S / 10000)。

        正式技能（SKILL_RULES 中定义）必须提交完整 metrics；未定义规则的
        技能走通用兜底，不启用技能专属门。
        无效练习：经验、等级、上次练习时间、指标累计值全部不变。
        """
        skill_id = _require_skill_id(skill_id)
        if proof_type not in _PROOF_TYPES:
            raise ValueError(f"未知 proof_type {proof_type!r}，可选 {_PROOF_TYPES}")
        if not isinstance(proof_url, str) or len(proof_url) > _PROOF_URL_MAX:
            raise ValueError(f"proof_url 必须是长度 <= {_PROOF_URL_MAX} 的字符串")
        if isinstance(buff_bps, bool) or buff_bps not in (10_000, 20_000):
            raise ValueError("buff_bps 只能是 10000 或 20000")
        if isinstance(survey_bps, bool) or not isinstance(survey_bps, int) \
                or not 8_000 <= survey_bps <= 11_200:
            raise ValueError("survey_bps 必须是 8000..11200 的整数")
        if survey is not None and not isinstance(survey, dict):
            raise ValueError("survey 必须是一个对象")
        with self._lock:
            t = self._now(now)
            # 首次自动建技能也必须落盘，否则 verify() 会报「存档中缺失」
            if self._ensure_skill(skill_id):
                self._mark_dirty()
            rule = skill_rule_for(skill_id, self._extra_rules)
            metric_verdict = None
            if rule is not None:
                metric_verdict = evaluate_skill_metrics(rule, metrics)
                if not metric_verdict.valid:
                    # 技能门不通过：零副作用返回，连指纹都不污染
                    snapshot = self._progress(skill_id, t)
                    rejected = PracticeVerdict(
                        False, metric_verdict.reason_code, 0,
                        skill_factor_bps=0, metric_checks=metric_verdict.checks,
                    )
                    return PracticeResult(rejected, snapshot, snapshot, 0, ())
            pre_events = self._settle_unlocked(skill_id, t)  # 先结算生疏
            before = self._progress(skill_id, t)
            state = self._states[skill_id]
            record = PracticeRecord(
                difficulty=difficulty, quality=quality, feedback=feedback,
                task_key=task_key, now=t, note=note,
            )
            # 同一 UTC 自然日内该技能的第几次有效练习，从只增事件日志算出，
            # 供「同日递减」使用；不新增可写状态，保证可回溯。
            today = day_index(t, self._tz_offset)
            same_day_count = 1 + sum(
                1 for ev in self._events
                if ev.skill_id == skill_id and ev.kind == "practice"
                and day_index(ev.at, self._tz_offset) == today
            )
            ctx = PracticeContext(
                level=state.level,
                retention_bps=state.retention_bps,
                last_practice_at=state.last_practice_at,
                recent_fingerprints=tuple(state.recent_fingerprints),
                low_streak=state.low_streak,
                same_day_count=same_day_count,
            )
            verdict = evaluate_practice(record, ctx,
                                        shape_enabled=self._shape_enabled)
            if verdict.valid and metric_verdict is not None:
                # 全整数四舍五入，避免浮点参与导致不同平台结果不一致
                scaled = max(1, (verdict.gain * metric_verdict.skill_factor_bps + 5_000)
                             // 10_000)
                verdict = replace(
                    verdict, gain=scaled,
                    skill_factor_bps=metric_verdict.skill_factor_bps,
                    metric_checks=metric_verdict.checks,
                )
            if verdict.valid and survey is not None:
                # v7 练习问卷：只微调最终增益（0.80–1.12 倍），
                # 不改变有效/无效判定，也不参与门槛统计。
                # 填了但正好 1.00 也写进 factors：让「这次问卷按 1.00 计」
                # 可被界面与事后核对看见，而不是看起来像没填。
                scaled = max(1, (verdict.gain * survey_bps + 5_000) // 10_000)
                verdict = replace(
                    verdict, gain=scaled,
                    factors={**verdict.factors, "survey_bps": survey_bps},
                )
            if verdict.valid and buff_bps != 10_000:
                # 悟道卡倍率叠加在通用增益与技能系数之后，全整数运算；
                # 只有有效练习才生效，且卡在服务层于练习成功后消耗一次。
                verdict = replace(
                    verdict, gain=(verdict.gain * buff_bps + 5_000) // 10_000
                )
            contaminated = verdict.reason_code in ("INVALID_INPUT", "TIME_REVERSED")

            if verdict.valid:
                ret_now = self._retention(state, t)
                state.retention_bps = min(10_000, ret_now + verdict.recovery_bps)
                state.total_exp += verdict.gain
                state.last_practice_at = t
                state.valid_sessions += 1
                state.quality_sum += quality
                state.feedback_max = max(state.feedback_max, FEEDBACK_LEVELS[feedback])
                state.max_difficulty = max(state.max_difficulty, difficulty)
                # v5 近期窗口：只留最近 window 条真实有效练习，用于截尾均值门槛。
                state.recent_sessions.append({
                    "at": t,
                    "quality": quality,
                    "difficulty": difficulty,
                    "feedback": FEEDBACK_LEVELS[feedback],
                    "metrics": dict(metrics) if metrics else {},
                })
                state.recent_sessions = state.recent_sessions[
                    -int(SHAPE_RULES["window"]):]
                if rule is not None:
                    for spec in rule.metrics:
                        key = spec.key
                        state.metric_sums[key] = (
                            state.metric_sums.get(key, 0) + int(metrics[key])
                        )
                        state.metric_max[key] = max(
                            int(state.metric_max.get(key, 0)), int(metrics[key]))
                # 连胜是独立维度：每天首次有效练习推进一次，同日重复不增长
                self._streak = advance_streak(
                    self._streak, t, tz_offset_seconds=self._tz_offset)
            if not contaminated:
                state.low_streak = (
                    state.low_streak + 1 if verdict.challenge == "low" else 0
                )
                state.recent_fingerprints = (
                    list(state.recent_fingerprints) + [str(task_key)]
                )[-REPEAT_WINDOW:]
                state.updated_at = _iso(t)
                state.settled_at = t   # 本次练习已按 t 结算
                # append-only 审计记录：有效练习记正增量，被判定无效的尝试记 0。
                # 非法输入（INVALID_INPUT / TIME_REVERSED）视为请求未被受理，不产生事件。
                self._record_event(
                    skill_id=skill_id, at=t,
                    kind="practice" if verdict.valid else "rejected",
                    delta=verdict.gain if verdict.valid else 0,
                    reason_code=verdict.reason_code, task_key=str(task_key),
                    difficulty=difficulty, quality=quality, feedback=feedback,
                    metrics=metrics,
                    proof_type=proof_type, proof_url=proof_url,
                    note=note, buff_bps=buff_bps if verdict.valid else 10_000,
                    survey_bps=survey_bps if verdict.valid else 10_000,
                    survey=survey if verdict.valid else None,
                )
                self._mark_dirty()

            post_events = self._settle_unlocked(skill_id, t)
            after = self._progress(skill_id, t)
            events = tuple(pre_events + post_events)
            ups, downs = list(self._listeners), list(self._down_listeners)
        self._dispatch(events, ups, downs)
        return PracticeResult(
            verdict=verdict, before=before, after=after,
            levels_gained=after.level - before.level, events=events,
        )

    def apply_baseline(
        self, skill_id: str, level: int, *, answers: dict | None = None,
        now: int | None = None,
    ) -> SkillProgress:
        """写入首登自评的「信用等级」（不增加 total_exp）。

        规则（防止自评变成刷分入口）：
          - 只在技能还是全新时允许：total_exp、valid_sessions 必须为 0，
            事件日志里不能有该技能的 practice / rejected 记录；
          - 等级上限由调用方按问卷口径收窄（服务层限制到 LV3）；
          - 重复提交同一等级是幂等的，不再追加事件；
          - 降级也允许：重新自评可以把起点调低，但不会低于 1。
        写入后由 _granted_level 把 baseline_level 当等级地板。
        """
        skill_id = _require_skill_id(skill_id)
        with self._lock:
            t = self._now(now)
            if self._ensure_skill(skill_id):
                self._mark_dirty()
            state = self._states[skill_id]
            top = self._tables[skill_id].config.max_level
            if isinstance(level, bool) or not isinstance(level, int):
                raise TypeError("baseline level 必须是整数")
            if not 1 <= level <= top:
                raise ValueError(f"baseline level 必须在 1..{top}")
            if state.total_exp > 0 or state.valid_sessions > 0:
                raise ValueError("该技能已有练习记录，不能再用自评设置起点")
            if any(ev.skill_id == skill_id and ev.kind in ("practice", "rejected")
                   for ev in self._events):
                raise ValueError("该技能已有练习事件，不能再用自评设置起点")
            if state.baseline_level == level:
                return self._progress(skill_id, t)
            state.baseline_level = level
            state.level = max(state.level, level)
            state.updated_at = _iso(t)
            payload = dict(answers or {})
            self._record_event(
                skill_id=skill_id, at=t, kind="admin", delta=0,
                reason_code="BASELINE_SELF_REPORT",
                task_key="首登自评",
                note=f"自评起点 LV{level}"[:500],
                survey=payload,
            )
            self._mark_dirty()
            return self._progress(skill_id, t)

    def set_archived(
        self, skill_id: str, archived: bool, *, now: int | None = None,
    ) -> SkillProgress:
        """归档 / 恢复一个技能（只影响展示与推荐，不动任何记录）。"""
        skill_id = _require_skill_id(skill_id)
        if not isinstance(archived, bool):
            raise TypeError("archived 必须是 bool")
        with self._lock:
            t = self._now(now)
            if self._ensure_skill(skill_id):
                self._mark_dirty()
            state = self._states[skill_id]
            state.archived = archived
            state.updated_at = _iso(t)
            self._mark_dirty()
            return self._progress(skill_id, t)

    def quiz_seen_ids(self, skill_id: str) -> list[str]:
        """已经出过的验证题（用于下次换题）。"""
        skill_id = _require_skill_id(skill_id)
        with self._lock:
            state = self._states.get(skill_id)
            return list(state.quiz_seen) if state else []

    def record_quiz(self, skill_id: str, correct_ids: list[str], *,
                    attempted_ids: list[str] = (), now: int | None = None) -> dict:
        """记一次验证题结果：答对的累计，满 3 题才放行；答错只关界面。

        返回 {"passed": bool, "credit": int, "needed": int, "progress": SkillProgress}。
        """
        skill_id = _require_skill_id(skill_id)
        ids = [str(x) for x in (correct_ids or [])]
        attempted = [str(x) for x in (attempted_ids or [])] or ids
        with self._lock:
            t = self._now(now)
            if self._ensure_skill(skill_id):
                self._mark_dirty()
            state = self._states[skill_id]
            progress = self._progress(skill_id, t)
            target = min(progress.exp_level, progress.level + 1)
            credit = min(3, int(state.quiz_credit) + len(ids))
            seen = list(state.quiz_seen) + attempted
            if credit >= 3:
                state.quiz_credit = 0
                state.quiz_seen = []
                state.quiz_retry_after = 0
                state.verified_level = max(int(state.verified_level), target)
                state.updated_at = _iso(t)
                self._settle_unlocked(skill_id, t)
                passed = True
            else:
                state.quiz_credit = credit
                state.quiz_seen = seen[-12:]
                # 答错后必须再完成一次有效练习才会重新出题
                state.quiz_retry_after = int(state.valid_sessions)
                passed = False
            self._mark_dirty()
            return {"passed": passed, "credit": credit, "needed": 3,
                    "target_level": target,
                    "progress": self._progress(skill_id, t)}

    def mark_verified(self, skill_id: str, level: int, *,
                      now: int | None = None) -> SkillProgress:
        """记录「已通过目标等级 level 的验证题」，并重算等级。"""
        skill_id = _require_skill_id(skill_id)
        if isinstance(level, bool) or not isinstance(level, int):
            raise TypeError("level 必须是整数")
        with self._lock:
            t = self._now(now)
            if self._ensure_skill(skill_id):
                self._mark_dirty()
            state = self._states[skill_id]
            top = self._tables[skill_id].config.max_level
            if not 1 <= level <= top:
                raise ValueError(f"level 必须在 1..{top}")
            if level > int(state.verified_level):
                state.verified_level = level
                state.updated_at = _iso(t)
            self._settle_unlocked(skill_id, t)
            self._mark_dirty()
            return self._progress(skill_id, t)

    def assess(self, skill_id: str, *, now: int | None = None) -> dict:
        """按可观测达标条件逐项判定当前等级是否「能力达标」。"""
        skill_id = _require_skill_id(skill_id)
        with self._lock:
            t = self._now(now)
            self._ensure_skill(skill_id)
            events = tuple(self._settle_unlocked(skill_id, t))
            if events:
                self._mark_dirty()
            progress = self._progress(skill_id, t)
            state = self._states[skill_id]
            stats = self._stats_unlocked(state, t)
            capped = min(progress.level, LEVEL_CRITERIA[-1]["level"])
            rule = skill_rule_for(skill_id, self._extra_rules)
            criteria = (assess_skill_level(rule, capped, stats, state.metric_sums)
                        if rule is not None else assess_level(capped, stats))
            # 卡级时给出「下一级缺哪几项」，让界面能解释为什么经验够却不升级
            next_criteria = None
            if progress.gate_blocked and rule is not None and capped < MAX_LEVEL:
                next_criteria = assess_skill_level(
                    rule, capped + 1, stats, state.metric_sums
                )
            ups, downs = list(self._listeners), list(self._down_listeners)
        self._dispatch(events, ups, downs)
        return {
            "skill_id": skill_id,
            "level": progress.level,
            "level_name": progress.level_name,
            "effective_exp": progress.effective_exp,
            "exp_level": progress.exp_level,
            "gate_blocked": progress.gate_blocked,
            "assess_gate_enabled": self._assess_gate,
            "stats": stats,
            "criteria": criteria,
            "next_level_criteria": next_criteria,
            "skill_rule": rule.skill_id if rule is not None else None,
            "metric_sums": dict(state.metric_sums),
            "window_used": bool(criteria.get("window_used")),
            "window_count": int(criteria.get("window_count", 0)),
            "fast_track": bool(criteria.get("fast_track")),
            "lifetime_metric_avgs": dict(
                criteria.get("lifetime_metric_avgs") or {}),
            "shape_enabled": self._shape_enabled,
        }

    def set_extra_rules(self, rules: dict | None) -> None:
        """热替换自定义技能规则表（增删自定义技能后由上层调用）。"""
        with self._lock:
            self._extra_rules = dict(rules or {})

    @property
    def extra_rule_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._extra_rules)

    # ---------- 写入 ----------

    def register_skill(self, skill_id: str, config: SkillConfig | None = None) -> SkillProgress:
        """注册/确保技能存在。已存在且未传 config 时为幂等空操作。"""
        skill_id = _require_skill_id(skill_id)
        with self._lock:
            if config is not None:
                return self.configure(skill_id, config)
            # 显式注册不受 strict 限制
            if skill_id not in self._states:
                self._configs[skill_id] = self._default_config
                self._tables[skill_id] = _LevelTable(self._default_config)
                self._states[skill_id] = SkillState(skill_id=skill_id, total_exp=0, level=1)
                self._mark_dirty()
            return self._progress(skill_id)

    def reset_skill(self, skill_id: str, *, now: int | None = None) -> SkillProgress:
        """把技能状态恢复为全新（清空累计经验、保持度、练习统计与任务指纹）。

        成长配置保留不变。用于「重置进度」而不是「删除技能」。
        """
        skill_id = _require_skill_id(skill_id)
        t = self._now(now)
        with self._lock:
            self._ensure_skill(skill_id)
            old_total = self._states[skill_id].total_exp
            self._states[skill_id] = SkillState(
                skill_id=skill_id, level=1, settled_at=t, updated_at=_iso(t)
            )
            # 重置是显式管理动作：只在确有经验被清空时记一条负增量事件，
            # 保证恒等式成立，同时避免零增量事件污染日志。
            if old_total:
                self._record_event(
                    skill_id=skill_id, at=t, kind="reset", delta=-old_total,
                    reason_code="reset_skill",
                )
            self._mark_dirty()
            return self._progress(skill_id, t)

    def remove_skill(self, skill_id: str) -> None:
        """移除技能及其配置与状态（用于清理旧版演示技能）。"""
        skill_id = _require_skill_id(skill_id)
        with self._lock:
            removed = any(skill_id in store for store in
                          (self._states, self._configs, self._tables))
            for store in (self._states, self._configs, self._tables):
                store.pop(skill_id, None)
            if removed:
                self._mark_dirty()

    def configure(self, skill_id: str, config: SkillConfig) -> SkillProgress:
        """热更新成长曲线（数值重平衡）。累计经验不变，等级按新曲线重算。"""
        skill_id = _require_skill_id(skill_id)
        if not isinstance(config, SkillConfig):
            raise TypeError("config 必须是 SkillConfig")
        with self._lock:
            self._configs[skill_id] = config
            self._tables[skill_id] = _LevelTable(config)
            if skill_id not in self._states:
                self._states[skill_id] = SkillState(skill_id=skill_id, total_exp=0, level=1)
            t = self._now()
            self._states[skill_id].settled_at = t
            self._states[skill_id].level = self._derived_level(
                skill_id, self._states[skill_id], t
            )
            self._mark_dirty()
            return self._progress(skill_id)

    def add_exp(self, skill_id: str, amount: int, *, reason: str = "",
                now: int | None = None) -> AddExpResult:
        """兼容旧接口的直接注入；视为「刚完成一次有效练习」。

        amount 必须是 >= 0 的整数。返回升级前后的完整快照与升级事件。
        满级后经验仍会累计到 total_exp，但等级不再变化。
        """
        skill_id = _require_skill_id(skill_id)
        amount = _require_int(amount, "amount", 0)
        t = self._now(now)
        with self._lock:
            self._ensure_skill(skill_id)
            before, after, events = self._apply(
                skill_id, self._states[skill_id].total_exp + amount, reason, t
            )
            listeners = list(self._listeners)
        # 回调异常向外抛出；状态此时已落盘，不会回滚
        self._emit(events, listeners)
        return AddExpResult(
            before=before,
            after=after,
            delta=amount,
            levels_gained=after.level - before.level,
            events=events,
        )

    def set_total_exp(self, skill_id: str, total_exp: int, *, reason: str = "set",
                      now: int | None = None) -> AddExpResult:
        """直接设定累计经验（补偿 / 回档 / 导入旧数据）。"""
        skill_id = _require_skill_id(skill_id)
        total_exp = _require_int(total_exp, "total_exp", 0)
        t = self._now(now)
        with self._lock:
            before, after, events = self._apply(skill_id, total_exp, reason, t)
            listeners = list(self._listeners)
        self._emit(events, listeners)
        return AddExpResult(before, after, total_exp - before.total_exp,
                           after.level - before.level, events)

    def set_level(self, skill_id: str, level: int,
                  now: int | None = None) -> SkillProgress:
        """把技能直接设到指定等级，本级经验归零。"""
        skill_id = _require_skill_id(skill_id)
        t = self._now(now)
        with self._lock:
            self._ensure_skill(skill_id)
            total = self._tables[skill_id].exp_for_level(level)
            _, after, events = self._apply(skill_id, total, "set_level", t)
            listeners = list(self._listeners)
        self._emit(events, listeners)
        return after

    # ---------- 查询 ----------

    def get_progress(self, skill_id: str, *, now: int | None = None) -> SkillProgress:
        skill_id = _require_skill_id(skill_id)
        with self._lock:
            t = self._now(now)
            # 查询触发的自动建技能也必须落盘，否则内存与存档会不一致
            if self._ensure_skill(skill_id):
                self._mark_dirty()
            events = tuple(self._settle_unlocked(skill_id, t))
            if events:
                self._mark_dirty()
            progress = self._progress(skill_id, t)
            ups, downs = list(self._listeners), list(self._down_listeners)
        self._dispatch(events, ups, downs)
        return progress

    def list_progress(self, *, now: int | None = None) -> dict[str, SkillProgress]:
        with self._lock:
            t = self._now(now)
            events: list = []
            for sid in self._states:
                events.extend(self._settle_unlocked(sid, t))
            if events:
                self._mark_dirty()
            result = {sid: self._progress(sid, t) for sid in self._states}
            ups, downs = list(self._listeners), list(self._down_listeners)
        self._dispatch(events, ups, downs)
        return result

    def get_config(self, skill_id: str) -> SkillConfig:
        skill_id = _require_skill_id(skill_id)
        with self._lock:
            if self._ensure_skill(skill_id):
                self._mark_dirty()
            return self._configs[skill_id]

    def snapshot(self) -> dict:
        """导出完整存档字典，便于发给客户端或做备份。"""
        with self._lock:
            return self._snapshot().to_dict()

    # ---------- 事件日志 / 连胜 / 统计（参考产品文章的「底层事件记录」） ----------

    def events(self, *, skill_id: str | None = None,
               limit: int | None = None,
               kinds: tuple[str, ...] | None = None) -> list[dict]:
        """按时间倒序读 append-only 事件日志。默认最新在前，不修改任何状态。"""
        with self._lock:
            rows = [e for e in self._events
                    if (skill_id is None or e.skill_id == skill_id)
                    and (kinds is None or e.kind in kinds)]
            rows = list(reversed(rows))
            if limit is not None:
                rows = rows[:max(0, _require_int(limit, "limit", 0))]
            return [e.to_dict() for e in rows]

    def reset_streak(self) -> dict:
        """清空连胜状态（事件日志按只增原则保留）。用于「重置全部进度」。"""
        with self._lock:
            self._streak = {}
            self._mark_dirty()
            return self.streak_info()

    def grant_freeze(self, count: int = 1) -> dict:
        """装备「连胜激冻」道具（显式仁慈机制，由利他层兑换后调用）。

        道具会在连胜即将中断且免费补签不可用/间隔更长时自动消耗，
        使连胜不降半、不清零。不改变任何增益数值。
        """
        n = _require_int(count, "count", 1)
        with self._lock:
            state = dict(self._streak)
            state["freeze_charges"] = int(state.get("freeze_charges", 0)) + n
            self._streak = state
            self._mark_dirty()
            return self.streak_info()

    @property
    def timezone_offset_seconds(self) -> int:
        """当前日历口径（秒）。0 = UTC；网页版默认 UTC+08:00。"""
        return self._tz_offset

    def set_timezone_offset(self, offset_seconds: int,
                            *, now: int | None = None) -> dict:
        """切换日历口径并重建连胜。只改日界线，不改经验、事件与保持度。

        重建依据为只增事件日志里的真实有效练习时间，不猜测旧天序号。
        """
        new = validate_tz_offset(offset_seconds)
        with self._lock:
            if new == self._tz_offset:
                return self.streak_info(now=now)
            old = self._tz_offset
            self._tz_offset = new
            self._streak = rebuild_streak(
                self._events, tz_offset_seconds=new, now=self._now(now),
                freeze_charges=self._streak.get("freeze_charges", 0))
            self._streak["timezone_migrated_from"] = old
            self._mark_dirty()
            return self.streak_info(now=now)

    def streak_info(self, *, now: int | None = None) -> dict:
        """读取连胜状态（不推进计数），并算出今天是否处于断签风险中。"""
        with self._lock:
            t = self._now(now)
            state = dict(self._streak)
            state.setdefault("current_days", 0)
            state.setdefault("grace_used", 0)
            state.setdefault("grace_remaining", STREAK_RULES["grace_per_window"])
            state.setdefault("freeze_charges", 0)
            state.setdefault("action", "none")
            state.setdefault("streak_bonus_bps", 0)
            last = state.get("last_day")
            gap = (day_index(t, self._tz_offset) - int(last)) \
                if last is not None else None
            state.update({
                "today_day": day_index(t, self._tz_offset),
                "tz_offset_seconds": self._tz_offset,
                "days_since_last": gap,
                "at_risk": gap is not None and gap >= 1,
                "grace_per_window": STREAK_RULES["grace_per_window"],
                "grace_window_days": STREAK_RULES["grace_window_days"],
                "bonus_enabled": bool(STREAK_RULES["bonus_enabled"]),
                "break_retain_divisor": STREAK_RULES["break_retain_divisor"],
            })
            return state

    def stats_summary(self, *, days: int = 30, now: int | None = None) -> dict:
        """近 N 个「本地日历日」的统计，全部由事件日志聚合，不另存汇总值。"""
        with self._lock:
            t = self._now(now)
            span = _require_int(days, "days", 1)
            if span > _MAX_STATS_DAYS:
                raise ValueError(f"days 不能超过 {_MAX_STATS_DAYS}")
            first = day_index(t, self._tz_offset) - (span - 1)
            daily: dict[int, int] = {}
            by_skill: dict[str, int] = {}
            counts = {"practice": 0, "rejected": 0, "admin": 0, "reset": 0}
            for e in self._events:
                d = day_index(e.at, self._tz_offset)
                if d < first:
                    continue
                daily[d] = daily.get(d, 0) + max(0, e.delta)
                if e.kind == "practice":
                    counts["practice"] += 1
                    by_skill[e.skill_id] = by_skill.get(e.skill_id, 0) + e.delta
                elif e.kind in counts:
                    counts[e.kind] += 1
            series = [{"day": first + i, "xp": daily.get(first + i, 0)}
                      for i in range(span)]
            return {
                "days": span, "series": series,
                "total_xp": sum(daily.values()),
                "by_skill": by_skill, "counts": counts,
                "event_count": len(self._events),
            }

    def event_ledger(self) -> dict[str, int]:
        """按技能汇总事件增量，用于核对 total_exp 与事件日志是否一致。"""
        with self._lock:
            ledger: dict[str, int] = {}
            for e in self._events:
                ledger[e.skill_id] = ledger.get(e.skill_id, 0) + e.delta
            return ledger

    def verify(self) -> list[str]:
        """自检：内存状态 vs 存档一致性、派生等级、事件日志恒等式。

        返回问题描述列表；空列表表示健康。
        """
        with self._lock:
            issues: list[str] = []
            saved = self._storage.load()
            if saved.tz_offset_seconds != self._tz_offset:
                issues.append(
                    f"时区口径未落盘：存档 {saved.tz_offset_seconds} "
                    f"!= 内存 {self._tz_offset}")
            if self._streak.get("last_day") is not None:
                practice_days = {
                    day_index(e.at, self._tz_offset)
                    for e in self._events if e.kind == "practice"}
                if practice_days and int(self._streak["last_day"]) \
                        not in practice_days:
                    issues.append("连胜最后一天在事件日志中找不到对应练习")
            for sid, state in self._states.items():
                # 缓存等级对应「最后结算时刻」，必须用该时刻复核，不能用当前时钟
                at = state.settled_at or self._now()
                derived = self._derived_level(sid, state, at)
                if state.level != derived:
                    issues.append(f"{sid}: 缓存等级 {state.level} != 派生等级 {derived}")
                saved_state = saved.states.get(sid)
                if saved_state is None:
                    issues.append(f"{sid}: 存档中缺失（可能需要 flush()）")
                elif saved_state.total_exp != state.total_exp:
                    issues.append(
                        f"{sid}: 存档经验 {saved_state.total_exp} != 内存 {state.total_exp}"
                    )
                if sid not in saved.configs:
                    issues.append(f"{sid}: 存档中缺失成长规则")
                if len(state.recent_sessions) > int(SHAPE_RULES["window"]):
                    issues.append(
                        f"{sid}: 近期窗口 {len(state.recent_sessions)} 条超过上限 "
                        f"{SHAPE_RULES['window']}"
                    )
            for sid in saved.states:
                if sid not in self._states:
                    issues.append(f"{sid}: 存档多出的技能，内存中不存在")
            # append-only 事件日志必须能解释每一个技能的累计经验
            ledger: dict[str, int] = {}
            for idx, e in enumerate(self._events, start=1):
                if e.seq != idx:
                    issues.append(f"事件序号不连续：第 {idx} 条 seq={e.seq}")
                ledger[e.skill_id] = ledger.get(e.skill_id, 0) + e.delta
            for sid, state in self._states.items():
                if ledger.get(sid, 0) != state.total_exp:
                    issues.append(
                        f"{sid}: 事件增量合计 {ledger.get(sid, 0)} != 累计经验 {state.total_exp}"
                    )
            if len(self._events) > _EVENT_LOG_WARN:
                issues.append(
                    f"事件日志 {len(self._events)} 条超过 {_EVENT_LOG_WARN}，建议归档（不自动截断以保恒等式）"
                )
            return issues

    # ---------- 事件与持久化控制 ----------

    def on_level_up(self, callback: Callable[[LevelUpEvent], None]) -> Callable[[], None]:
        """订阅升级事件，返回取消订阅函数。"""
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def on_level_down(self, callback: Callable[[LevelDownEvent], None]) -> Callable[[], None]:
        """订阅生疏导致的等级退化事件，返回取消订阅函数。"""
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        with self._lock:
            self._down_listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._down_listeners:
                    self._down_listeners.remove(callback)

        return unsubscribe

    def flush(self) -> None:
        """把内存状态写入存储。auto_save=False 时必须手动调用。"""
        with self._lock:
            self._flush()

    @contextmanager
    def batch(self) -> Iterator["ProficiencySystem"]:
        """合并多次写入为一次落盘，退出时自动 flush。"""
        with self._lock:
            self._batch_depth += 1
        try:
            yield self
        finally:
            with self._lock:
                self._batch_depth -= 1
                if self._batch_depth == 0:
                    self._flush()

    def __enter__(self) -> "ProficiencySystem":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.flush()
        return False
