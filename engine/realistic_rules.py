# -*- coding: utf-8 -*-
"""现实生活技能成长规则 · 规则定义与判定标准（纯函数，可复现）。

本文件是整个系统的「规格」。所有函数都是纯函数：输入显式给定，
不读系统时钟、不读全局状态、不做随机。同一组输入永远得到同一输出，
因此任何判定结果都能用单元测试逐条复现。

规则总览
--------
1. 只有「有效练习」计入熟练度。一次练习由 4 个可观测输入描述：
   任务难度 difficulty(1-5)、完成质量 quality(0-100)、
   实际反馈 feedback(none/self/external/measured)、任务标识 task_key。
2. 单次有效经验 = 基础值(20 x 难度) x 质量系数 x 反馈系数
   x 难度匹配系数 x 重复系数，Decimal 精确计算后四舍五入取整，最少 1 点。
3. 等级阈值按幂律递增 cost(L) = ceil(base x L^k)，k>1，
   因此相同质量的练习在越高等级带来的等级进展越小 —— 提升先快后慢。
4. 熟练度分两层：total_exp 是历史累计有效经验（单调不减，学了不会忘光）；
   retention_bps 是保持度（0-10000 基点）。当前有效熟练度
   effective_exp = total_exp x retention_bps // 10000，等级由它派生。
   长期搁置时保持度按分段线性锚表下降，最低保底 3500 bps（肌肉记忆）。
5. 中断后有效练习会回补保持度，因此等级回升远快于初学；
   这与「重新从零练」有本质区别。
6. 升级：结算后 effective_exp 跨过下一级阈值。
   退化：结算后 effective_exp 跌破当前等级下限阈值。
   两者都由显式 settle(now) 触发，不靠隐式定时器，保证可复现。
7. 每个熟练等级另有一组「可观测达标条件」（有效练习次数、平均质量、
   反馈强度、最高完成难度、允许搁置天数），由 assess_level() 逐项判定，
   用于校验「经验达标」是否伴随「能力可观测达标」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

__all__ = [
    "FEEDBACK_LEVELS", "FEEDBACK_FACTOR_BPS", "CHALLENGE_FACTOR_BPS",
    "MIN_QUALITY", "NO_FEEDBACK_QUALITY_GATE", "LOW_STREAK_LIMIT",
    "REPEAT_WINDOW", "DECAY_GRACE_SECONDS", "DECAY_ANCHORS",
    "RETENTION_FLOOR_BPS",
    "LEVEL_CRITERIA", "REASON_CODES",
    "PracticeRecord", "PracticeContext", "PracticeVerdict",
    "level_band", "classify_challenge", "retention_cap_bps",
    "current_retention_bps", "effective_exp", "recovery_bps",
    "evaluate_practice", "assess_level", "rulebook",
    "SKILL_IDS", "MetricSpec", "SkillRule", "MetricVerdict", "SKILL_RULES",
    "evaluate_skill_metrics", "assess_skill_level", "skill_rule_for",
    "MAX_LEVEL", "STANDARD_SOURCES", "DECISION_POINTS", "MEDICAL_KEYS",
    "NO_OFFICIAL_MAPPING", "SELF_DEFINED_MAPPING",
    "RPE_MIN", "RPE_MAX", "RPE_VIGOROUS",
    "RPE_REJECT_ABOVE", "PLAIN_RULES",
    "OPEN_DECISIONS", "APPLIED_DECISIONS", "IMPLEMENTATION_NOTES",
    "collect_open_items", "expand_evidence", "open_decision_report",
    "STREAK_RULES", "ARTICLE_FACT_CHECKS", "day_index", "grace_window",
    "streak_bonus_bps", "advance_streak", "rebuild_streak",
    "TZ_OFFSET_MIN", "TZ_OFFSET_MAX", "validate_tz_offset",
    "REALM_TITLES", "ACHIEVEMENTS", "evaluate_achievements",
    "ULIVES_REFERENCE", "SOURCE_VERIFICATION", "TRUST_RULES",
    "NARRATIVE_RULES", "ALTRUISM_RULES", "REPUTATION_TITLES",
    "ALTRUISM_REASON_CODES", "SHAPE_RULES", "DIAGNOSIS_VERIFICATION",
    "window_summary", "quick_pass",
    "LOW_FRICTION_RULES", "CAPABILITY_VERIFICATION",
    "PROFICIENCY_DEFINITION", "REVIEW_VERIFICATION",
    "SETTLEMENT_FORMS", "SETTLEMENT_FORM_META",
    "SETTLEMENT_DIFFICULTY_CHOICES", "SETTLEMENT_QUALITY_CHOICES",
    "SETTLEMENT_FEEDBACK_CHOICES", "SETTLEMENT_SAFETY_QUESTIONS",
    "metric_choices", "settlement_form", "settlement_form_book",
]

# --------------------------------------------------------------------------
# 常量表（改动这里等于改规则，必须同步改测试）
# --------------------------------------------------------------------------

DAY_SECONDS = 86_400

# --------------------------------------------------------------------------
# 2026-11-08 诊断修正：依据 compare.py 实跑与「报告.html」逐条反向核实。
# 学理只作为设计动机，具体系数全部是本系统自定参数，不是论文结论。
# --------------------------------------------------------------------------
DIAGNOSIS_VERIFICATION: tuple[dict, ...] = (
    {
        "claim": "按「刚好达到下一级标准」练，因终身均值永远差一点而升不了级",
        "verdict": "成立（已实跑复现）",
        "evidence": "英语口语每次按 LV3 标准练（8 分钟 / 5 个表达 / 理解度 70），60 次后终身均值为 7 / 4 / 69，等级停在 LV2、经验等级 5、卡级。数学上 avg(n)=T−k(T−a)/n < T 对任意有限 n 成立。",
        "action": "门槛均值改「近期窗口截尾均值」+「连续 3 次逐次达标快通」；终身均值降级为历程页履历，不再当门。",
    },
    {
        "claim": "难度 5 / 质量 90 / 客观度量 = +113 点，一次吃掉本级 113%、一次升一级",
        "verdict": "增益数值成立，『一次升一级』不成立",
        "evidence": "实跑 gain=113、累计经验 113（本级成本 100）；但已拍板的 assess_gate=hard_gate 要求 LV2 至少 3 次有效练习，实测等级仍 LV1 且 gate_blocked=True。",
        "action": "用钟形难度系数修正填表套利；升级照走硬门槛，不绕过。",
    },
    {
        "claim": "一天连刷 20 次与分散 20 天完全相同（896/896）",
        "verdict": "成立",
        "evidence": "compare.py 实跑 896/896=1.00；8 周同总量「每周 1 天×3 次」与「每周 3 天×1 次」为 1088/1088=1.00。",
        "action": "新增同日递减与 1–7 天间隔加成（只加成、不打折，符合『不加惩罚』红线）。",
    },
    {
        "claim": "报告表格与图表的「提案」列：LV2@5 · LV3@13 · LV4@21 · LV5@30",
        "verdict": "与 compare.py 实跑不一致",
        "evidence": "该脚本实跑提案列为 LV2:6 / LV3:14 / LV4:24 / LV5 从未达到（4000 次上限内）；图表标注『质量 85、客观度量』但 scen_typical 实际用 quality=80 / external。报告图表与表格的提案列不是该脚本输出。",
        "action": "本轮以实跑为准，改完直接重跑真实系统取数，不复用提案模型里的手填数字。",
    },
    {
        "claim": "间隔效应是学习科学里最坚固的结论之一",
        "verdict": "定性成立，但需限定领域",
        "evidence": "Cepeda et al. 2006（Psychological Bulletin，317 实验 / 184 篇）、Dunlosky et al. 2013（PSPI，分布练习评为 high utility）、Donovan & Radosevich 1999（d=0.46）、Mawson & Kang 2025 课堂（d=0.54）。研究集中在言语回忆与课堂记忆任务，不是运动技能熟练度。",
        "action": "采纳「分散优于突击」的方向；1.15 / 0.6 等系数是本系统自定参数，不是论文数值。",
    },
    {
        "claim": "目标梯度：人一旦知道只差一点，行动率显著上升",
        "verdict": "有出处，但属消费奖励领域",
        "evidence": "Hull 1932/1934 目标梯度假说；Kivetz, Urminsky & Zheng 2006（JMR 43(1):39–58）咖啡集章实验中购买间隔缩短约 20%，预先盖 2 章的 12 章卡比空白 10 章卡完成更快。",
        "action": "用于「今日推荐卡 / 里程碑」的界面设计动机；不把 20% 当作本系统的效果承诺。",
    },
    {
        "claim": "报告建议的「超纲自动压低质量上限」「稳固度缩放衰减」「主动恢复日」",
        "verdict": "有据取舍，本轮部分采纳",
        "evidence": "质量截断会改写用户上报值，与「数据准确、不篡改上报」口径冲突；钟形系数已把 +113 降到 +14（提案实跑），目的达成。稳固度需重标定已拍板的 decay_anchors=keep；恢复日需新增练习类型。",
        "action": "不实现质量改写；稳固度与恢复日做成显式可配置项并在规则页如实标注状态，不假装已实现。",
    },
)

# 增益与门槛的「形状修正」参数。enabled=False 可完整回退到 2026-11-08 之前的
# 判定口径（用于对照与回归），但不修复终身均值死锁。
SHAPE_RULES: dict = {
    "enabled": True,
    "window": 10,                 # 达标校验只看最近 10 次有效练习
    "trim": 2,                    # 去掉最差 2 次后取均值（截尾均值）
    "quick_streak": 3,            # 最近 N 次逐次全部达标 → 免除均值类门槛
    # 难度相对当前等级带的钟形系数（bps）。delta = difficulty - level_band(level)
    "difficulty_bps": {-2: 1_500, -1: 5_000, 0: 10_000,
                       1: 8_500, 2: 4_500, 3: 2_000},
    "same_day_step_bps": 6_000,   # 同技能当日第 n 次 → 10000/(10000+6000(n-1))
    "spacing_bps": {1: 11_500, 2: 11_500, 3: 11_500,
                    4: 10_500, 5: 10_500, 6: 10_500, 7: 10_500},
    "rest_day_enabled": False,    # 「主动恢复日」未实现，显式标注
    "stability_scaling_enabled": False,  # 「稳固度缩放衰减」未实现，显式标注
    "disclaimer": (
        "窗口大小、截尾数、快通次数、钟形系数、同日递减与间隔加成全部是本系统"
        "自定的可调参数；学理依据只支持「分散优于突击」这个方向，不支持任何具体数值。"
    ),
}


def window_summary(recent_sessions, *, window: int | None = None,
                   trim: int | None = None) -> dict | None:
    """近期窗口截尾均值。纯函数，输入为每次有效练习的记录列表。

    返回 avg_quality / feedback_max / max_difficulty / metric_avgs / count；
    无记录时返回 None，调用方据此回退到终身累计口径（保证旧存档兼容）。
    """
    w = int(SHAPE_RULES["window"] if window is None else window)
    t = int(SHAPE_RULES["trim"] if trim is None else trim)
    rows = list(recent_sessions or [])[-w:]
    if not rows:
        return None

    def trimmed(values) -> int:
        vals = sorted(int(v) for v in values)
        if len(vals) > t:
            vals = vals[t:]
        return sum(vals) // len(vals) if vals else 0

    metric_keys = {k for row in rows for k in (row.get("metrics") or {})}
    return {
        "count": len(rows),
        "avg_quality": trimmed(row.get("quality", 0) for row in rows),
        "feedback_max": max(int(row.get("feedback", 0)) for row in rows),
        "max_difficulty": max(int(row.get("difficulty", 0)) for row in rows),
        "metric_avgs": {
            key: trimmed((row.get("metrics") or {}).get(key, 0) for row in rows)
            for key in metric_keys
        },
        "rows": rows,
    }


def quick_pass(rows, level: int, rule=None, *, need: int | None = None) -> bool:
    """连续 N 次逐次全部满足目标等级的可观测条件 → 快通（免除均值类门槛）。

    只免除「平均质量 / 指标均值」；累计次数、反馈强度、最高难度、最长搁置
    仍照常校验，避免 3 次练习直达 LV8。
    """
    n = int(SHAPE_RULES["quick_streak"] if need is None else need)
    criteria = next((c for c in LEVEL_CRITERIA if c["level"] == level), None)
    if criteria is None or len(rows) < n:
        return False
    recent = list(rows)[-n:]
    for row in recent:
        if int(row.get("quality", 0)) < criteria["min_avg_quality"]:
            return False
        if int(row.get("feedback", 0)) < criteria["min_feedback"]:
            return False
        if int(row.get("difficulty", 0)) < criteria["min_difficulty"]:
            return False
        if rule is not None:
            idx = min(max(level, 1), len(LEVEL_CRITERIA)) - 1
            values = row.get("metrics") or {}
            for spec in rule.metrics:
                if int(values.get(spec.key, 0)) < spec.level_requirements[idx]:
                    return False
    return True


# --------------------------------------------------------------------------
# 连胜维度（参考产品文章的机制建议）
# 全部数值是 MVP 默认参数，不是实证结论，也不是多邻国的真实参数。
# 时间口径：day_index(unix, tz_offset_seconds)。偏移为 0 时与旧版 UTC 口径
# 逐位一致；网页版注入本地偏移（默认 UTC+08:00），避免早上 8 点换日。
# --------------------------------------------------------------------------
TZ_OFFSET_MIN = -14 * 3600
TZ_OFFSET_MAX = 14 * 3600


def validate_tz_offset(offset_seconds: int) -> int:
    """时区偏移合法性。纯函数；范围 ±14 小时覆盖全球民用时区。"""
    if (isinstance(offset_seconds, bool)
            or not isinstance(offset_seconds, int)
            or not (TZ_OFFSET_MIN <= offset_seconds <= TZ_OFFSET_MAX)):
        raise ValueError(
            f"时区偏移必须是 {TZ_OFFSET_MIN}..{TZ_OFFSET_MAX} 之间的整数秒")
    return offset_seconds


STREAK_RULES: dict[str, int | bool | str] = {
    "day_seconds": DAY_SECONDS,
    "timezone": "可配置（0 = UTC；网页版默认 UTC+08:00）",
    "grace_window_days": 7,       # 每 7 个 UTC 天为一个宽限窗口
    "grace_per_window": 1,        # 每窗口允许 1 次「断 1 天」自动补签
    "break_retain_divisor": 2,    # 超出宽限后连胜降半保留，不清零
    "bonus_bps_per_day": 50,      # 连胜加成系数（仅计算并记录，默认不生效）
    "bonus_cap_bps": 2000,        # 加成上限 +20%
    "bonus_enabled": False,       # 默认关闭：启用会推翻已拍板的 gain_weights=keep
}


def day_index(unix_seconds: int, tz_offset_seconds: int = 0) -> int:
    """按给定时区偏移计算「日历天序号」。同一天内的多次完成只算一天。

    offset=0 时与历史 UTC 口径逐位一致（兼容性契约）。
    偏移只平移日界线，不改变 Unix 时间戳本身，也不影响 total_exp、
    保持度与事件日志恒等式；它只影响按「日」聚合的判定与展示：
    同日递减、连胜、每日声望上限、今日所得、近 N 日统计。
    """
    return (int(unix_seconds) + validate_tz_offset(tz_offset_seconds)) // DAY_SECONDS


def grace_window(day: int) -> int:
    """宽限窗口序号；窗口变化时补签次数重置。"""
    return int(day) // int(STREAK_RULES["grace_window_days"])


def streak_bonus_bps(streak_days: int) -> int:
    """连胜加成（基点）。纯函数；是否生效由 STREAK_RULES.bonus_enabled 决定。"""
    raw = max(0, int(streak_days)) * int(STREAK_RULES["bonus_bps_per_day"])
    return min(raw, int(STREAK_RULES["bonus_cap_bps"]))


def advance_streak(state: dict, today_unix: int,
                   *, tz_offset_seconds: int = 0) -> dict:
    """推进连胜。纯函数，不读系统时钟、不修改入参。

    tz_offset_seconds 只影响「今天」落在哪个日历日；默认 0 保持旧 UTC 行为。

    state 字段：current_days / last_day / window / grace_used / freeze_charges
    action：first 首次 / same_day 同日重复 / extended 正常延续 /
            grace 断 1 天用掉免费补签 / freeze_used 消耗「连胜激冻」道具 /
            broken_retained 中断后降半保留
    freeze_charges 默认 0，此时行为与 v3 完全一致，不改变已拍板规则。
    """
    today = day_index(today_unix, tz_offset_seconds)
    window = grace_window(today)
    current = int(state.get("current_days", 0))
    last = state.get("last_day")
    used = int(state.get("grace_used", 0)) if int(state.get("window", window)) == window else 0
    charges = int(state.get("freeze_charges", 0))
    limit = int(STREAK_RULES["grace_per_window"])
    divisor = int(STREAK_RULES["break_retain_divisor"])

    if last is None:
        current, action = 1, "first"
    else:
        gap = today - int(last)
        if gap <= 0:
            action = "same_day"
        elif gap == 1:
            current, action = current + 1, "extended"
        elif gap == 2 and used < limit:
            used, action = used + 1, "grace"   # 免费补签：连胜不断，也不增长
        elif charges > 0:
            # 显式装备的「连胜激冻」：可覆盖更长间隔，连胜不减半、不清零
            current, action, charges = max(current, 1), "freeze_used", charges - 1
        else:
            current = current // divisor + 1  # 降半保留 + 今天这一天
            action = "broken_retained"
    result = {
        "current_days": current,
        "last_day": int(last) if last is not None else today,
        "window": window,
        "grace_used": used,
        "freeze_charges": charges,
        "grace_remaining": max(0, limit - used),
        "action": action,
        "streak_bonus_bps": streak_bonus_bps(current),
        "bonus_enabled": bool(STREAK_RULES["bonus_enabled"]),
    }
    if action == "same_day":
        result["last_day"] = int(last)
    else:
        result["last_day"] = today
    return result


def rebuild_streak(events, *, tz_offset_seconds: int = 0,
                   now: int = 0, freeze_charges: int = 0) -> dict:
    """时区口径变更后，从只增事件日志重建连胜。纯函数。

    旧存档只存「天序号」，没有绝对时间戳对应的口径信息；改时区后
    last_day 无法可靠平移。这里不猜测，直接用真实有效练习的 at 按新偏移
    重算连续天数。补签次数与历史 action 不迁移（重建后归零），
    freeze_charges 是道具数量，与日历无关，原样保留。
    """
    rows = []
    for e in events:
        at = int(e["at"]) if isinstance(e, dict) else int(e.at)
        kind = e.get("kind") if isinstance(e, dict) else e.kind
        if kind == "practice":
            rows.append(at)
    days = sorted({day_index(at, tz_offset_seconds) for at in rows})
    current = 0
    if days:
        current = 1
        for i in range(len(days) - 1, 0, -1):
            if days[i] - days[i - 1] == 1:
                current += 1
            else:
                break
    window = grace_window(day_index(int(now), tz_offset_seconds))
    return {
        "current_days": current,
        "last_day": days[-1] if days else None,
        "window": window,
        "grace_used": 0,
        "freeze_charges": max(0, int(freeze_charges)),
        "grace_remaining": int(STREAK_RULES["grace_per_window"]),
        "action": "timezone_rebuilt" if days else "none",
        "streak_bonus_bps": streak_bonus_bps(current),
        "bonus_enabled": bool(STREAK_RULES["bonus_enabled"]),
    }


# --------------------------------------------------------------------------
# 网文式境界称号（ulives「自定义等级名称」机制）
# 纯展示文案，不参与任何判定；官方等级名仍是 LEVEL_CRITERIA.name（对齐 EQF）。
# --------------------------------------------------------------------------
REALM_TITLES: tuple[str, ...] = (
    "初窥门径", "登堂入室", "渐入佳境", "融会贯通",
    "炉火纯青", "登峰造极", "超凡入圣", "一代宗师",
)


def realm_title(level: int) -> str:
    idx = min(max(int(level), 1), len(REALM_TITLES)) - 1
    return REALM_TITLES[idx]


# 自动成就（参考 ulives 的「自动/周期型成就」形态）。
# 阈值全部是本系统自定的 MVP 参数，不是 ulives 的真实配置，也不是任何标准。
ACHIEVEMENTS: tuple[dict, ...] = (
    {"id": "first_step", "name": "初入江湖", "desc": "完成第 1 次有效练习",
     "metric": "sessions", "target": 1, "icon": "一"},
    {"id": "ten_sessions", "name": "小有所成", "desc": "累计 10 次有效练习",
     "metric": "sessions", "target": 10, "icon": "十"},
    {"id": "fifty_sessions", "name": "勤修不辍", "desc": "累计 50 次有效练习",
     "metric": "sessions", "target": 50, "icon": "百"},
    {"id": "streak_3", "name": "三日不辍", "desc": "连续 3 天完成练习",
     "metric": "streak", "target": 3, "icon": "三"},
    {"id": "streak_7", "name": "一周圆满", "desc": "连续 7 天完成练习",
     "metric": "streak", "target": 7, "icon": "七"},
    {"id": "streak_30", "name": "月满功成", "desc": "连续 30 天完成练习",
     "metric": "streak", "target": 30, "icon": "卅"},
    {"id": "xp_1000", "name": "千点真元", "desc": "累计获得 1000 点熟练度",
     "metric": "total_xp", "target": 1000, "icon": "千"},
    {"id": "daily_200", "name": "一日精进", "desc": "单日获得 200 点熟练度",
     "metric": "best_day_xp", "target": 200, "icon": "日"},
    {"id": "any_lv5", "name": "名动一方", "desc": "任一技能达到 LV5",
     "metric": "max_level", "target": 5, "icon": "五"},
    # 质成就：关于「做到以前做不到的事」，而不是「又打开了一次页面」。
    {"id": "metric_pb", "name": "破己记录", "desc": "同一任务的主指标创个人最好（至少练过 2 次）",
     "metric": "personal_bests", "target": 1, "icon": "破"},
    {"id": "benchmark_pass", "name": "复测见真章",
     "desc": "同一基准任务按同口径复测，主指标超过基线",
     "metric": "benchmark_passes", "target": 1, "icon": "证"},
)


def evaluate_achievements(context: dict) -> list[dict]:
    """按事件聚合值判定成就。纯函数，不读时钟、不改状态。

    context 允许字段：sessions / streak / total_xp / best_day_xp / max_level
    以及质成就用的 personal_bests / benchmark_passes（缺省按 0，旧调用方不受影响）。
    """
    rows = []
    for item in ACHIEVEMENTS:
        actual = int(context.get(item["metric"], 0) or 0)
        target = int(item["target"])
        pct = min(100.0, round(actual / target * 100, 2)) if target > 0 else 100.0
        rows.append({
            **item, "actual": actual,
            "progress_percent": pct,
            "unlocked": actual >= target,
            "remaining": max(0, target - actual),
        })
    return rows


# 参考 ulives（LifeUp 团队《人升》2.0）后的机制对照。来源是本次联网实抓的
# 官方站点与 App Store 描述；未采纳项如实写出理由，不假装已经实现。
ULIVES_REFERENCE: dict = {
    "product": "ulives（读作 you-lives），LifeUp 团队核心成员开发的跨平台游戏化待办 / 习惯 App，亦称《人升》2.0",
    "sources": [
        "https://app.ulives.io/",
        "https://ulives.lifeupapp.net/zh-Hans/",
        "https://apps.apple.com/cn/app/id6743084261",
        "https://ulives.io",
    ],
    "adopted": [
        {"mechanism": "自定义属性 / 技能，完成即获 XP 并升级",
         "here": "7 个技能各自独立累计与等级，等级由经验派生"},
        {"mechanism": "自定义等级名称",
         "here": "新增 8 个网文式境界称号（纯展示文案，不参与判定）"},
        {"mechanism": "自动成就 / 连续打卡成就，进度可视化",
         "here": "9 个自动成就，由事件日志与连胜状态纯函数判定"},
        {"mechanism": "活动日志时间轴",
         "here": "append-only 事件日志，可回溯、可核对 sum(delta)=total_exp"},
        {"mechanism": "统计与进度可视化",
         "here": "近 7 / 30 日 XP 趋势、技能分布、连胜与宽限状态"},
        {"mechanism": "本地优先、离线可用",
         "here": "本地 JSON / SQLite 存档，服务只绑 127.0.0.1，不联网"},
    ],
    "not_adopted": [
        {"mechanism": "金币、奖励商店、战利品箱（自定义概率）",
         "reason": "会引入第二套货币与随机奖励，和本系统「数据可复现、不随机」的判定原则冲突"},
        {"mechanism": "番茄钟 / 专注计时联动奖励",
         "reason": "需要计时器与前台常驻，超出当前本地网页服务范围"},
        {"mechanism": "多配置档（profiles）",
         "reason": "当前按单实例单存档设计；多档需要改造存储主键"},
        {"mechanism": "任务 / 子任务 / 重复提醒",
         "reason": "本系统记录「完成事件」而非管理待办清单，避免变成第二个 To-do App"},
    ],
}


# 对参考文章的反向核实结果。机制可借鉴，数字不可照抄；网页同源导出。
ARTICLE_FACT_CHECKS: tuple[dict, ...] = (
    {
        "claim": "XP_for_level(N) = floor(1000 * N^1.5)，5 级约 2236、10 级约 3162",
        "verdict": "公式与数字自相矛盾",
        "evidence": "实跑：1000*5^1.5=11180、1000*10^1.5=31622；2236 与 3162 实际是 1000*sqrt(N)。且 sqrt 曲线的每级成本递减（414/318/268/…），与文章自己说的『先快后慢』相反。",
        "action": "不采用该公式；沿用现有 PowerRule(100,1.5)，每级成本 100/283/520/800/1119/… 递增。",
    },
    {
        "claim": "iOS 连胜小组件使用户承诺度提升 60%",
        "verdict": "官方来源无此数字",
        "evidence": "Duolingo 官方博客 blog.duolingo.com/widget-feature/ 全文只有『装了小组件的学习者中一半有 ≥6 个月连胜』和『控制选择偏差后留存仍更好』，未给 60%；且官方明确提示装小组件的人本来就更投入。60% 出自第三方营销博客转述。",
        "action": "只采纳『把连胜放到显眼位置』这个设计动作，不把 60% 写进系统或当作效果承诺。",
    },
    {
        "claim": "损失厌恶：失去的痛苦约为同等获得快乐的 2 倍",
        "verdict": "有学术出处，但不是普适定律",
        "evidence": "Tversky & Kahneman 1992 累积前景理论给出损失厌恶系数中位数约 2.25；1979 前景理论提出 losses loom larger than gains。2022 年起有多国学者质疑其解释力与适用范围。",
        "action": "作为连胜『损失感』的设计动机；具体宽限参数由本系统自定，不从这个倍数推导。",
    },
    {
        "claim": "蔡格尼克效应驱动进度条",
        "verdict": "类比，不是该效应的原始定义",
        "evidence": "蔡格尼克效应原指『对未完成/被打断任务的记忆优于已完成任务』（经典实验回忆率 68% vs 43%），是记忆效应，不是进度条动机。",
        "action": "进度条照做（现有系统已有），但不拿这个效应当科学依据。",
    },
)

# --------------------------------------------------------------------------
# 2026-11-08 本轮「信任 / 上瘾 / 利他」三层意见的联网反向核实。
# 原则：机制可以借鉴，未经一手来源证实的数字与专名一律不写进系统当事实。
# 每条带 verdict（结论）/ evidence（实抓证据）/ action（本系统怎么做）。
# --------------------------------------------------------------------------
SOURCE_VERIFICATION: tuple[dict, ...] = (
    {
        "layer": "信任", "claim": "Consistify 是首款要求提供证据的习惯应用，实时拍照禁相册、AI 评委判定",
        "verdict": "产品真实，『首款』是商店自述",
        "evidence": "Google Play 应用页确实写有 AI judge、Only real-time photos work, no gallery uploads allowed；但同批检索到 Proof: AI Habit Tracker、即刻习惯等同类产品同样做实时拍照与本地识别，『首款』无法证实。Consistify 下载量标注 10+，属极早期产品。",
        "action": "采纳『实时拍摄优于相册上传』的机制思路，只做 proof_type/proof_url 字段预留；不引用『首款』，不声称已实现 AI 判定。",
    },
    {
        "layer": "信任", "claim": "SkillHunt 用生物识别面部扫描绑定身份，每次提交与个人资料匹配",
        "verdict": "产品存在，功能描述对不上",
        "evidence": "2026-04-14 上线的 SkillHunt 自述为 daily photo verification + Peer-reviewed by the community + Karma；官方文案里没有『面部扫描绑定身份、每次提交匹配个人资料』的功能描述，『biometric accountability arena / your face is your proof』是营销措辞。另存在三个同名无关产品（招聘面试平台、Agent 技能发现平台等）。",
        "action": "不采纳生物识别说法；本系统不做任何身份绑定，明确标注 proof 只是声明元数据。",
    },
    {
        "layer": "信任", "claim": "HabitWar 小组投票 / ChallengeUp 团队互助打卡",
        "verdict": "均真实存在且描述吻合",
        "evidence": "HabitWar 官网与 Product Hunt 写明 add photo proof, join votes to keep everyone honest；ChallengeUp 应用页写明每日 photo/video 证明、公开 Challenge Score 0–100（完成率/同伴评分/打卡一致性）、help each other stay on track。",
        "action": "采纳『同伴注视优于纯荣誉系统』作为设计动机；本机无真实同伴，peer_vote 仅作为一种 proof_type 声明，不做投票流程。",
    },
    {
        "layer": "上瘾", "claim": "拟人化推送（猫头鹰眼含泪光）使次日留存率暴增 23%",
        "verdict": "查无 23% 这一数字",
        "evidence": "可核实的一手/权威数字是：Duolingo 官方博客（日文）给出放宽到 2 个连胜激冻后 DAU +0.38%；KDD 2020 论文（Yancey & Settles）通知 bandit 算法 DAU +0.5%、新用户 7 日留存 +2.0%；2017 Streak Wager 7 日留存 +14%；媒体引述冯安/WSJ 为留存 +3%。没有 23%，也没有『眼含泪光使次日留存』的一手数据。",
        "action": "剔除 23%；保留『人格化系统文案』作为界面表现，不写效果承诺。本地网页服务无系统推送能力，只做页面内文案。",
    },
    {
        "layer": "上瘾", "claim": "连胜激冻是多邻国被使用最多的功能之一",
        "verdict": "功能真实，『使用最多』无官方排名",
        "evidence": "Duolingo 官方帮助页确认连胜激冻（Streak Freeze）与连胜修复为真实功能；官方博客说明其设计理由（引用宾大/UCLA 关于『给目标留余地』的研究），并给出放宽到最多 2 个后 DAU +0.38%。另可核实到 Silverman & Barasch (2023, JCR)：完整连胜提升参与、断裂降低参与、可修复时影响更小；Lally et al. (2010)：漏一天不显著影响习惯形成。",
        "action": "采纳『显式宽限道具』并新增连胜激冻；不写『使用最多』。激冻与已有自动补签并存：补签免费覆盖断 1 天，激冻可覆盖更长间隔。",
    },
    {
        "layer": "上瘾", "claim": "Habitica 惩罚机制被学术研究指出存在反生产力效应（忙碌高产期反而更易被罚）",
        "verdict": "惩罚功能真实，『学术研究』未找到一手论文",
        "evidence": "Habitica 官方 Wiki 确认 Dailies 未完成扣血、并提供 Pause Damage；但检索到的『惩罚反生产力』论述均来自评测博客、Reddit 讨论与竞品营销文，未找到同行评议研究，更没有『忙碌高产期更易被罚』这一具体结论。",
        "action": "采纳『惩罚必须克制』作为保守设计原则，但不声称有学术结论背书。本系统确认：无扣血、无扣分、无负向经验；唯一负向是长期搁置的自然生疏（现实规律，已拍板保留），且配有 1 天宽限、连胜宽限与恢复加速。",
    },
    {
        "layer": "上瘾", "claim": "「天道」Demo：性格测试绑定系统、每日任务、六维雷达图、星座技能树",
        "verdict": "真实存在，但属竞赛 Demo 非上线产品",
        "evidence": "TRAE 官方中文社区大赛初赛作品，React + Vite 纯前端 SPA；5 道性格测试匹配 8 类系统、每日 5 个现实任务、六维属性 Canvas 雷达图、星座式技能树、连胜与成就、LocalStorage 持久化，与描述完全吻合。",
        "action": "采纳『清晰路径感 + 即时身份代入』的叙事外壳（宿主 / 境界 / 主线任务 / 系统提示），不复制性格测试与六维属性（会引入第二套与已拍板规则冲突的数值体系）。",
    },
    {
        "layer": "利他", "claim": "GP-AIR 框架：量化参与者声誉并在群体中共享，激发内在利他动机",
        "verdict": "查无此框架名；间接互惠概念本身真实",
        "evidence": "检索不到名为 GP-AIR 的框架。真实存在的是 indirect reciprocity（Nowak & Sigmund 2005）、leading-eight 策略，以及群体声誉模型（PLOS Comput Biol 2024, Kawakatsu et al.; J. R. Soc. Interface 2025）。这些是演化博弈模型，不是可直接套用的产品数值。",
        "action": "不采用 GP-AIR 名称。声望维度标注为『基于间接互惠思路的系统自定实现』，全部汇率与门槛为本机 MVP 参数，不引用任何论文数字。",
    },
    {
        "layer": "利他", "claim": "「能量球复活」式救场 / 团队宗门模式",
        "verdict": "名称无出处，相近机制真实",
        "evidence": "未检索到『能量球复活』的可核实产品出处。相近的真实机制是多邻国 Friend Streak（2024）与 Streak Revival（2026-06，Q2 2026 股东信称 1540 万人使用）；ChallengeUp 的互助打卡也是真实产品。",
        "action": "不采用该名称。护道机制独立实现，并明确标注为本机多角色模拟、不联网、没有真实社区可见性。",
    },
)

# 验证层：MVP 只做最轻的字段预留，不做图片/生物特征/AI 判定。
TRUST_RULES: dict = {
    "proof_type_values": ("", "live_photo", "peer_vote", "external"),
    "proof_type_labels": {
        "": "无（自报）", "live_photo": "声明·实时拍摄",
        "peer_vote": "声明·同伴复核", "external": "声明·外部系统",
    },
    "proof_url_max_len": 512,
    "verification_implemented": False,
    "declaration": (
        "本机只记录 proof_type / proof_url 两个元数据字段，不接收图片、不存储文件、"
        "不下载链接、不校验真伪、不做 AI 判定或身份绑定。trusted 恒为 False。"
        "真实校验需接入外部证据服务；字段预留的目的是让历史记录将来可被补验，"
        "而不是现在就声称『完成已被证明』。"
    ),
    "source_ids": ("consistify", "habitwar", "challengeup"),
}

# --------------------------------------------------------------------------
# 低摩擦采集层：解决「每次打卡要填太多字段」的真实痛点。
# 立身之本：候选时段绝不自动折算熟练度；所有加分仍走 add_practice 的原判定门。
# 能力边界：本机网页只能看见「自己这一页」是否可见，读不到手机屏幕状态、
#   读不到前台是哪个 App、无法在后台持续运行。下列条目均经一手来源反向核实。
# --------------------------------------------------------------------------
CAPABILITY_VERIFICATION: tuple[dict, ...] = (
    {
        "claim": "网页能读取手机屏幕是亮是灭、以及用户正在用哪个 App",
        "verdict": "不成立",
        "source": "MDN · Page Visibility API",
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API",
        "note": ("浏览器只暴露 document.visibilityState（visible/hidden），描述的是"
                 "「本页自己」是否可见。切标签、最小化、切到其他 App、锁屏熄屏都会"
                 "得到同一个 hidden，无法区分原因，更拿不到前台应用包名。"),
    },
    {
        "claim": "页面切到后台后还能持续记录练了多久",
        "verdict": "不成立",
        "source": "MDN · visibilitychange / 后台限流",
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/Document/visibilitychange_event",
        "note": ("MDN 明确：转为 hidden 是页面能可靠观察到的最后一个事件；后台标签的"
                 " setTimeout 被预算限流、requestAnimationFrame 停止。因此只能在"
                 "「回到页面时」补齐区间，不能宣称后台持续监控。"),
    },
    {
        "claim": "安卓原生可以读到应用使用时长，网页也照搬即可",
        "verdict": "条件成立但不可照搬",
        "source": "Android Developers · UsageStatsManager",
        "url": "https://developer.android.com/reference/android/app/usage/UsageStatsManager",
        "note": ("该 API 需要 android.permission.PACKAGE_USAGE_STATS，且用户必须在系统"
                 "「使用情况访问权限」里手动授予；它是原生 API，网页无权调用。"
                 "本项目是本地网页服务，不是原生 App。"),
    },
    {
        "claim": "用无障碍服务读屏幕内容就能自动判断在练什么",
        "verdict": "技术上可行但被平台政策限制，且本项目不具备",
        "source": "Google Play · 使用 AccessibilityService API",
        "url": "https://support.google.com/googleplay/android-developer/answer/10964491",
        "note": ("非无障碍工具的应用必须在 App 内显著披露、取得用户明确同意、并在 Play"
                 " 管理中心申报；政策禁止由应用自主发起、规划及执行动作。"
                 "且屏幕文字本身无法给出难度、质量、反馈等本系统判定所需的观测值。"),
    },
    {
        "claim": "iOS 有官方接口能实时知道前台 App / 用户在做什么",
        "verdict": "不成立",
        "source": "Apple Developer Forums · DTS 工程师答复",
        "url": "https://developer.apple.com/forums/thread/706481",
        "note": ("Apple DTS 明确答复：后台或已终止时无法检测屏幕事件、无法取得当前"
                 "活动名；Screen Time API 被刻意设计成「第三方 App 提供家长控制，"
                 "但不获知用户具体在做什么」。另一答复亦确认无实时前台应用"
                 " bundle ID 的官方接口（https://developer.apple.com/forums/thread/837850）。"),
    },
    {
        "claim": "静默采集屏幕使用记录不涉及个人信息合规问题",
        "verdict": "不成立",
        "source": "中国人大网 · 中华人民共和国个人信息保护法 全文",
        "url": "http://www.npc.gov.cn/npc/c2/c30834/202108/t20210820_313088.html",
        "note": ("第六条：收集个人信息限于实现目的的最小范围，不得过度收集；"
                 "第十三/十四条：基于同意的处理需充分知情、自愿明确；"
                 "第二十八/二十九条：敏感个人信息须特定目的、充分必要并单独同意；"
                 "第五十五条：处理敏感个人信息或自动化决策需事前做影响评估。"
                 "因此本功能不采集屏幕内容、不采集前台应用、不联网上传。"),
    },
    {
        "claim": "「屏幕亮了 40 分钟」等于「练了 40 分钟英语口语」",
        "verdict": "不成立（识别问题，非权限问题）",
        "source": "本系统判定口径",
        "url": "",
        "note": ("本系统每次加分要求技能专属观测值（如连续表达时长、主动使用新表达、"
                 "可理解度）与难度/质量/反馈。屏幕状态不携带任何这些信息，"
                 "因此即使拿到全部权限也无法由它推定熟练度。"),
    },
)

LOW_FRICTION_RULES: dict = {
    "enabled": True,
    "auto_credit": False,
    "signal_source": "own_page_visibility",
    "signals_allowed": ("manual_start", "page_hidden", "page_visible", "manual_stop"),
    "min_session_seconds": 60,
    "max_session_seconds": 6 * 3600,
    "pending_ttl_days": 7,
    "max_pending": 20,
    "max_active": 1,
    "questionnaire_mode": "per_skill",
    "max_questions": 12,
    "max_questions_note": ("技能专属问卷一屏内可点完：每题都有默认选中值、大按钮选项，"
                           "不改动也能一次确认；上限 12 题（运动类含 3 道安全门）。"),
    "prefill_source": "last_valid_practice_event",
    "prefill_inherit": ("difficulty", "quality", "feedback", "metrics", "task_key"),
    "prefill_never_inherit": ("proof_type", "proof_url", "note"),
    "prefill_disclaimer": ("预填值来自该技能上一次【有效练习】的自报记录，"
                           "不是本次重新测量；沿用必须由你点一次确认，"
                           "事件里会写明「沿用上次自评」。"),
    "quick_templates": (
        "沿用上次：一次确认，把上次有效练习的自评值作为本次自报值（事件里标明未重新测量）",
        "换了任务：数值沿用，任务标识自动带本次日期，避免被机械重复规则误伤",
        "自己填：展开完整表单，本次重新填写全部观测值（运动安全门必须走这条）",
    ),
    "skip_policy": "跳过 / 关闭 / 过期 / 点「不算」= 熟练度不变，不扣分、不自动补记、不推送打扰",
    "task_key_policy": ("任务标识默认沿用上次；若这次确实换了任务，可在第二问一键换成"
                        "带日期的新标识。系统不校验任务真伪，机械重复仍按既有"
                        " REPEATED_TASK 规则拒绝。"),
    "no_auto_credit_note": ("挂机时长只是【候选证据】，不折算经验、不改等级、不推连胜。"
                            "只有你在结算卡上点一次确认，才会走 add_practice 的全部判定门。"),
    "boot_behavior": ("启动时若发现未收口的挂机，自动把它转成「待结算」卡片；"
                      "超过单次上限的直接作废，不补分。"),
    "plain": (
        "1. 开工只点一下，之后该干嘛干嘛。系统只记「这个页面在前台待了多久」，"
        "不读你手机屏幕、不知道你开了哪个 App。",
        "2. 挂机时长不会自己变成熟练度。它只是一张待确认的小票，"
        "你不点头，一分都不加。",
        "3. 收工后按技能出一份结算问卷：学英语读写就问做了哪套卷子、读了多少词、"
        "写了多少词、正确率、卷子难不难；健身就问练了什么、几组、每组几次、"
        "强度、反馈，还有疼痛 / RPE / 停止信号三项安全门。",
        "4. 问卷里的每个问题，都对应系统早就算好的一个判定字段。"
        "没有新标准、没有新阈值：难度 1–5、反馈 4 档、完成质量 4 档，"
        "各技能的最低线和加成线还是原来那套。",
        "5. 每题都是大按钮选项并已默认选中，点一下就能改；嫌麻烦可以一个字不填直接确认，"
        "也可以按「和上次一样」一键结算。不改动直接确认会记成「本次问卷自报」，"
        "不冒充仪器测量。",
        "6. 难度不是越高越好：系统会拿它和当前等级比，太简单或明显超纲都会压低收益，"
        "连续第 3 次低难度直接判无效。",
        "7. 跳过、关掉、放超过 7 天、或者点「不算」，都只是这张小票作废，不扣分、不补记。",
        "8. 确认之后照样过原来的所有关卡：指标最低线、质量门槛、反馈、重复任务、"
        "运动安全门一个不少。快捷不等于放水。",
    ),
    "not_implemented": (
        "真实手机屏幕状态 / 前台应用识别",
        "后台常驻与系统推送",
        "图像或 AI 自动验真、生物识别",
        "真实社区、好友联网与数据同步",
    ),
}

# --------------------------------------------------------------------------
# 结算问卷：开工只点一下，收工后按技能问「这次到底练了什么」。
#
# 关键约束（反向验证前提）：这里【不新增任何标准、阈值或系数】。
# 每个问题都只是把已有的判定字段翻译成人话：
#   MetricSpec.*          -> 该技能的观测值问题
#   difficulty (1-5)      -> 「这套东西对你有多难」→ 钟形难度系数 + 难度匹配
#   feedback (4 档)       -> 「怎么知道做得对不对」→ FEEDBACK_FACTOR_BPS
#   quality (0-100)       -> 「完成得怎么样」→ 质量门槛与增益
#   task_key              -> 「做了什么材料 / 训练」→ 任务标识与重复判定
#   note                  -> 随记，进同一任务的证据线
#   医学安全门三项        -> 疼痛 / Borg RPE / 停止信号（运动类必问）
# 快捷选项的数值全部由 MetricSpec 的 range_low / min_valid / good / range_high
# 派生，不手写数字；技能专属部分只有「问法」和「常见任务选项」。
# --------------------------------------------------------------------------
SETTLEMENT_DIFFICULTY_CHOICES: tuple[dict, ...] = (
    {"value": 1, "label": "1 · 很轻松"},
    {"value": 2, "label": "2 · 有点容易"},
    {"value": 3, "label": "3 · 刚好"},
    {"value": 4, "label": "4 · 吃力"},
    {"value": 5, "label": "5 · 明显超纲"},
)
SETTLEMENT_QUALITY_CHOICES: tuple[dict, ...] = (
    {"value": 30, "label": "敷衍（可能不计入）"},
    {"value": 60, "label": "一般"},
    {"value": 80, "label": "到位"},
    {"value": 95, "label": "超常发挥"},
)
SETTLEMENT_FEEDBACK_CHOICES: tuple[dict, ...] = (
    {"value": "measured", "label": "成绩 / 计时 / 仪器"},
    {"value": "external", "label": "别人点评 / 教练"},
    {"value": "self", "label": "自己核对答案"},
    {"value": "none", "label": "没人看，不知道对错"},
)
SETTLEMENT_SAFETY_QUESTIONS: tuple[dict, ...] = (
    {"key": "pain_flag", "label": "这次有没有疼痛或不适？",
     "hint": "报了疼痛，本次按安全门判为无效，不计入熟练度",
     "default": 0,
     "options": ({"value": 0, "label": "没有"},
                 {"value": 1, "label": "有（本次不计入）"})},
    {"key": "rpe", "label": "这次有多累（Borg RPE 6–20）？",
     "hint": "Borg 主观用力等级；超过 17 属极高强度，本次判为无效",
     "default": 12,
     "options": ({"value": 9, "label": "9 · 很轻松"},
                 {"value": 12, "label": "12 · 有点吃力"},
                 {"value": 14, "label": "14 · 剧烈"},
                 {"value": 18, "label": "18 · 过头了（不计入）"})},
    {"key": "stop_sign", "label": "有没有胸口疼、头晕、喘不上气这类信号？",
     "hint": "ACSM 停止运动警示信号；出现即本次判为无效",
     "default": 0,
     "options": ({"value": 0, "label": "没有"},
                 {"value": 1, "label": "有（本次不计入）"})},
)

SETTLEMENT_FORMS: dict[str, dict] = {
    "英语口语": {
        "intro": "这次开口说了什么？",
        "task_label": "这次练的是哪种口语任务？",
        "task_options": ("跟读模仿", "自由口语 / 自述", "和人对练",
                         "复述 / 演讲", "模考口试"),
        "metrics": {
            "continuous_minutes": {
                "label": "连续说了多少分钟（中间没停下的那种）？",
                "hint": "系统标尺，不是 CEFR 规定值；低于 1 分钟不计入"},
            "active_phrases": {
                "label": "这次主动用了几个新表达？",
                "hint": "自己主动说出来才算，照着稿念不计；低于 1 个不计入"},
            "listener_score": {
                "label": "对方听懂了多少（0-100）？",
                "hint": "按听者反馈或录音复核自评；低于 60 不计入"},
        },
        "difficulty_label": "这次话题对你有多难？",
        "note_placeholder": "例：第一次连续说到 6 分 20 秒",
    },
    "英语读写": {
        "intro": "这次做的哪套材料 / 卷子，读了多少、写了多少、对了几成？",
        "task_label": "这次做的是哪套材料或卷子？",
        "task_options": ("精读 + 写作练习", "真题卷 / 模考卷", "外刊精读",
                         "课文与课后作业", "自由写作"),
        "metrics": {
            "words_read": {
                "label": "这套材料读了多少词？",
                "hint": "系统练习量标尺；低于 50 词不计入"},
            "comprehension_score": {
                "label": "卷子 / 题目的正确率（0-100）？",
                "hint": "按可核对的题目或批改结果填；本机不校验真伪；低于 60 不计入"},
            "words_written": {
                "label": "这次写了多少词？",
                "hint": "系统练习量标尺；低于 30 词不计入"},
        },
        "difficulty_label": "这套卷子 / 材料对你有多难？",
        "note_placeholder": "例：2024 年真题阅读 Section 2，错了 3 题",
    },
    "西班牙语": {
        "intro": "这次西语练了哪一块，说了多久、模考对了几成、读了多少？",
        "task_label": "这次做的是哪类西语练习？",
        "task_options": ("DELE 模考", "口语对练", "西语阅读",
                         "课文与语法", "听写 / 复述"),
        "metrics": {
            "oral_minutes": {
                "label": "连续说了多少分钟西语？",
                "hint": "系统标尺；低于 1 分钟不计入"},
            "dele_mock_score": {
                "label": "DELE 模考正确率（0-100）？",
                "hint": "模考分数不等于正式 DELE 证书；低于 60 不计入"},
            "words_read": {
                "label": "西语读了多少词？",
                "hint": "系统练习量标尺；低于 30 词不计入"},
        },
        "difficulty_label": "这次材料对你有多难？",
        "note_placeholder": "例：DELE B1 阅读模拟，错 4 题",
    },
    "电脑办公软件技术": {
        "intro": "这次做了哪些任务，完成几个、结果对不对、手上有什么认证？",
        "task_label": "这次做的是什么任务？",
        "task_options": ("表格公式 / 函数", "长文档排版", "图表与数据透视",
                         "批量处理 / 自动化", "模板搭建"),
        "metrics": {
            "tasks_completed": {
                "label": "这次独立完成了几个任务？",
                "hint": "系统计数标尺；低于 1 个不计入"},
            "accuracy_score": {
                "label": "操作结果正确率（0-100）？",
                "hint": "按能核对的结果自评；低于 60 不计入"},
            "cert_level": {
                "label": "你目前持有的最高认证？",
                "hint": "映射为本系统约定，非微软官方等级编号；0 也合法",
                "value_labels": {0: "无", 1: "ICDL",
                                 2: "MOS Associate", 3: "MOS Expert"}},
        },
        "difficulty_label": "这批任务对你有多难？",
        "note_placeholder": "例：用 XLOOKUP 重做月报，核对了 20 行",
    },
    "剪辑技术": {
        "intro": "这次交付了几件，验收过没过，成片自己打几分？",
        "task_label": "这次交付的是什么？",
        "task_options": ("短视频成片", "中长视频", "客户返工修改",
                         "片头 / 包装", "素材整理与归档"),
        "metrics": {
            "deliverables": {
                "label": "这次实际交付了几件作品？",
                "hint": "按可核对交付物计，不按成片时长折算；低于 1 件不计入"},
            "client_acceptance": {
                "label": "客户 / 需求方验收通过率（0-100）？",
                "hint": "按验收记录填；本机只记自报值，不校验真伪；低于 60 不计入"},
            "delivery_score": {
                "label": "交付质量评分（0-100）？",
                "hint": "按自报处理，系统不接收第三方评分；低于 60 不计入"},
        },
        "difficulty_label": "这个项目对你有多难？",
        "note_placeholder": "例：3 条口播成片，客户改了 1 条",
    },
    "跑步技术": {
        "intro": "这次跑的什么课、多远、步频多少，身体有没有报警？",
        "task_label": "这次跑的是哪种课？",
        "task_options": ("轻松跑", "间歇 / 速度", "节奏跑",
                         "长距离", "技术动作练习"),
        "metrics": {
            "distance_meters": {
                "label": "跑了多少米？",
                "hint": "距离只是练习量标尺，不是官方能力等级；低于 500 米不计入"},
            "cadence_spm": {
                "label": "平均步频（步 / 分）？",
                "hint": "ACSM：较习惯步频提高 5–10% 通常使步频 >170，且无通用金标准；低于 140 不计入"},
            "form_score": {
                "label": "技术动作自评（0-100）？",
                "hint": "系统不采集视频；低于 60 不计入"},
        },
        "difficulty_label": "这次训练强度对你有多难？",
        "note_placeholder": "例：第 3 公里落地偏前，后面刻意收步幅",
    },
    "健身技术": {
        "intro": "这次练了什么、做了几组、每组几次，身体有没有报警？",
        "task_label": "这次做了什么训练？",
        "task_options": ("上肢推 / 拉", "下肢蹲 / 髋", "核心与稳定",
                         "全身循环", "力量主项 + 辅助"),
        "metrics": {
            "working_sets": {
                "label": "有效做了几组？",
                "hint": "ACSM 2026：力量 2–3 组 / 动作，增肌约 10 组 / 肌群 / 周；低于 1 组不计入"},
            "reps_per_set": {
                "label": "每组大约几次？",
                "hint": "ACSM 2009：新手 8–12RM；低于 5 次不计入"},
            "form_score": {
                "label": "动作质量自评（0-100）？",
                "hint": "系统不采集视频；低于 60 不计入"},
        },
        "difficulty_label": "这次训练强度对你有多难？",
        "note_placeholder": "例：深蹲 60kg 4×8，最后一组略变形",
    },
}

SETTLEMENT_FORM_META: dict = {
    "questionnaire_mode": "per_skill",
    "difficulty_hint": (
        "难度是 1–5 的系统标尺，默认按你当前等级给出，请按这次材料 / 训练的真实难度改。"
        "系统会拿它跟当前等级比：刚好匹配收益最高，太简单或明显超纲都会被钟形系数压低；"
        "连续第 3 次低难度练习直接判无效。"
    ),
    "task_hint": ("任务名写进记录，用于同一任务的纵向证据线；"
                  "最近 3 次都是同一个任务会触发机械重复判定。"
                  "选「自己写」可填具体卷子 / 训练名。"),
    "disclaimer": (
        "问卷只是把系统已有的判定字段翻译成人话：所有最低线、加成线、合法范围、"
        "系数与等级条件都来自 SKILL_RULES 与 LOW_FRICTION_RULES，没有新增标准。"
        "快捷选项的数值全部由 MetricSpec 派生；每题都有默认选中值，"
        "不改动直接确认也算一次「本次问卷自报」，不冒充仪器测量。"
    ),
    "field_map": {
        "metric 题": "SKILL_RULES 的 required_keys → add_practice.metrics",
        "难度": "1–5 → add_practice.difficulty（钟形难度 + 难度匹配）",
        "反馈": "4 档 → add_practice.feedback（FEEDBACK_FACTOR_BPS）",
        "完成质量": "4 档 → add_practice.quality（质量门槛 + 增益）",
        "任务": "选项或自填 → add_practice.task_key",
        "随记": "→ add_practice.note（进同一任务证据线）",
        "安全门三项": "仅运动技能 → add_practice.metrics 的 pain_flag / rpe / stop_sign",
    },
}


def metric_choices(spec: MetricSpec,
                   value_labels: dict[int, str] | None = None) -> list[dict]:
    """把某个观测指标变成几个「一点就选」的档位。纯函数。

    数值只取声明过的 range_low / min_valid / good / 中间值 / range_high，
    去重后返回；不手写任何新阈值，避免问卷口径和判定口径漂移。
    """
    mid = spec.min_valid + (spec.good - spec.min_valid) // 2
    candidates = [spec.range_low, spec.min_valid, mid,
                  spec.good, spec.range_high]
    seen: set[int] = set()
    out: list[dict] = []
    for v in candidates:
        if v in seen:
            continue
        seen.add(v)
        if value_labels and v in value_labels:
            label = value_labels[v]
        elif v == spec.min_valid:
            label = f"刚达标 · {v} {spec.unit}"
        elif v == spec.good:
            label = f"加成线 · {v} {spec.unit}"
        elif v == spec.range_high:
            label = f"上限 · {v} {spec.unit}"
        elif v < spec.min_valid:
            label = f"低于有效线 · {v} {spec.unit}"
        else:
            label = f"{v} {spec.unit}"
        out.append({"value": v, "label": label})
    return out


def settlement_form(skill_id: str, *, level: int = 1,
                    prefill: dict | None = None,
                    rule: SkillRule | None = None,
                    form_spec: dict | None = None) -> dict:
    """生成某技能的结算问卷。纯函数；默认值来自真实事件预填或保守下限。

    prefill 是 prefill_from_event 的结果（可为 None）。没有可用预填时，
    观测值默认取 min_valid（刚达标），不默认是「表现很好」。
    """
    # 自定义技能由调用方按租户注入规则与问卷规格，不改模块级全局表。
    rule = rule or skill_rule_for(skill_id)
    spec = form_spec or SETTLEMENT_FORMS.get(skill_id)
    if rule is None or spec is None:
        raise KeyError(f"技能 {skill_id!r} 没有结算问卷规格")
    values = dict((prefill or {}).get("values") or {}) \
        if (prefill or {}).get("available") else {}
    metric_values = dict(values.get("metrics") or {})
    questions: list[dict] = []

    for m in rule.metrics:
        qspec = spec["metrics"][m.key]
        options = metric_choices(m, qspec.get("value_labels"))
        default = metric_values.get(m.key, m.min_valid)
        if isinstance(default, bool) or not isinstance(default, int) \
                or not (m.range_low <= default <= m.range_high):
            default = m.min_valid
        if default not in [o["value"] for o in options]:
            options.append({"value": default,
                            "label": f"沿用上次 · {default} {m.unit}"})
        questions.append({
            "key": m.key, "group": "metric", "kind": "number_choice",
            "label": qspec["label"], "hint": qspec["hint"],
            "unit": m.unit, "min": m.range_low, "max": m.range_high,
            "min_valid": m.min_valid, "good": m.good,
            "options": options, "default": default,
        })

    if rule.medical_gate:
        for sq in SETTLEMENT_SAFETY_QUESTIONS:
            questions.append({
                "key": sq["key"], "group": "safety", "kind": "choice",
                "label": sq["label"], "hint": sq["hint"],
                "options": [dict(o) for o in sq["options"]],
                "default": sq["default"],
            })

    task_default = values.get("task_key") or spec["task_options"][0]
    questions.append({
        "key": "task", "group": "task", "kind": "task_choice",
        "label": spec["task_label"], "hint": SETTLEMENT_FORM_META["task_hint"],
        "options": [{"value": x, "label": x} for x in spec["task_options"]],
        "default": task_default, "allow_custom": True,
        "custom_placeholder": "自己写：哪套卷子 / 哪个训练",
    })
    questions.append({
        "key": "difficulty", "group": "judgement", "kind": "choice",
        "label": spec["difficulty_label"],
        "hint": SETTLEMENT_FORM_META["difficulty_hint"],
        "options": [dict(o) for o in SETTLEMENT_DIFFICULTY_CHOICES],
        "default": level_band(level),
    })
    questions.append({
        "key": "feedback", "group": "judgement", "kind": "choice",
        "label": "这次怎么知道做得对不对？",
        "hint": "客观度量加成最高，无反馈且质量不高会直接判为无效",
        "options": [dict(o) for o in SETTLEMENT_FEEDBACK_CHOICES],
        "default": "self",
    })
    questions.append({
        "key": "quality", "group": "judgement", "kind": "choice",
        "label": "这次完成得怎么样？",
        "hint": "质量低于 40 不构成有效练习；无反馈时须 >= 70",
        "options": [dict(o) for o in SETTLEMENT_QUALITY_CHOICES],
        "default": 80,
    })
    questions.append({
        "key": "note", "group": "note", "kind": "text",
        "label": "这一手记一笔（可留空）",
        "hint": "会留在同一任务的时间线里，方便以后纵向对比",
        "default": "", "placeholder": spec["note_placeholder"],
    })
    return {
        "skill_id": skill_id,
        "intro": spec["intro"],
        "question_count": len(questions),
        "questions": questions,
        "difficulty_hint": SETTLEMENT_FORM_META["difficulty_hint"],
        "disclaimer": SETTLEMENT_FORM_META["disclaimer"],
        "reuse_available": bool((prefill or {}).get("available")),
        "submit_hint": ("确认后仍走原有全部判定门：指标最低线、质量、反馈、"
                        "重复任务、运动安全门、同日递减，一个不少。"),
    }


def settlement_form_book() -> dict:
    """给规则页与接口用的同源问卷清单。默认等级 LV1，无预填。"""
    return {sid: settlement_form(sid) for sid in SKILL_IDS}

# --------------------------------------------------------------------------
# 熟练度的定义、分级与成长规则（规则页与接口同源，避免文档与代码漂移）
# --------------------------------------------------------------------------
PROFICIENCY_DEFINITION: dict = {
    "what_it_is": (
        "熟练度是「一个人在某项现实技能上，能被可观测证据支持的稳定表现水平」"
        "的本系统量化记账。它不是练习时长，不是打卡次数，也不是自报的感觉。"
    ),
    "what_it_is_not": (
        "不是官方资格认证：语言 LV1–LV6 锚 CEFR/DELE can-do，办公锚 ICDL/MOS 阶梯，"
        "剪辑 LV1–LV2 锚 Adobe ACP 指南，运动只锚 Borg RPE/ACSM 安全门；"
        "官方未覆盖的等级一律标注「自建标准·非官方」或「无官方对应」。",
        "不是仪表读数：所有观测值由本人自报，proof_type / proof_url 只是声明元数据，"
        "本机不收图、不下载链接、不校验真伪，trusted 恒为 False。",
        "不是永久保持：长期搁置按保持度自然生疏，这是已拍板的现实规律，不是惩罚。",
    ),
    "three_layers": {
        "累计经验 total_exp": "只增不减；每次有效练习的增量来自 append-only 事件日志，满足 sum(delta)==total_exp",
        "有效熟练度 effective_exp": "total_exp × 保持度(bps) // 10000；决定经验等级",
        "实际等级 level": "经验等级还要逐级通过可观测达标校验才放行；未通过即卡级（gate_blocked）",
    },
    "grading": {
        "range": "LV1–LV8，名称对齐 EQF 描述符（入门→权威）",
        "cost": "PowerRule: ceil(base × L^k)，默认 k=1.5；越高越慢",
        "gate": "每级都有 min_sessions / min_avg_quality / min_feedback / min_difficulty / max_idle_days 五道通用门槛，"
                "外加该技能 3 个专属指标的均值门槛；近期窗口用截尾均值，连续快通可免均值类门槛",
        "downgrade": "保持度下降使有效熟练度低于当前等级阈值时退化，最低 LV1；1 天宽限期避免边界抖动",
    },
    "growth": {
        "entry": "add_practice 是唯一入口；候选时段 / 问卷结算 / 网页打卡都走这里",
        "gate_order": ["技能指标门（完整/范围/安全门/最低线）", "输入合法性", "时间不倒流",
                       "质量门槛", "无反馈+低质量", "连续低难度", "机械重复任务", "计分"],
        "gain": "max(1, round(20×难度 × 质量/100 × 反馈系数 × 难度匹配 × 重复折扣 × 同日递减 × 间隔加成 × 技能系数))",
        "invalid": "被判定无效的练习：经验、等级、上次练习时间、指标累计全部不变（零副作用）",
        "daily_rule": "同日递减与连胜按【可配置的本地日历日】判定，默认 UTC+08:00；偏移只改日界线，不改任何时间戳与经验",
    },
    "invariants": (
        "事件日志只增，seq 从 1 连续",
        "每个技能 sum(event.delta) == state.total_exp",
        "累计经验单调不减；等级不会低于 LV1",
        "缓存等级必须等于「最后结算时刻」重算出的派生等级",
        "管理注入（add_exp/set_level）显式绕过练习达标校验，并记录 admin 事件",
    ),
    "disclaimer": (
        "阈值、系数、窗口、截尾数、声望汇率均为本系统自定的可调参数；"
        "学理依据只支持「分散优于突击」「先快后慢」「长期搁置生疏」这些方向，"
        "不支持任何具体数值。不要把本系统的数字当成实证结论。"
    ),
}

# 本轮外部审查报告的逐条反向核实（实跑代码 / 读代码后写，不照抄报告数字）
REVIEW_VERIFICATION: tuple[dict, ...] = (
    {
        "claim": "day_index 用 UTC 日，导致北京时间早上 8 点换日；同日递减两头错",
        "verdict": "成立，已修",
        "evidence": ("review_sim.py 实跑：北京 23:30→次日 07:30（间隔 8h）在 UTC 口径下"
                     " same_day_bps=6250、增益 16；07:30→09:00（间隔 1.5h）反而是 10000、增益 25。"
                     "修为 UTC+08:00 后前者 10000/25、后者 6250/16。"),
        "action": "day_index(ts, tz_offset_seconds)；系统注入并存 v6 存档；同日递减/连胜/统计/江湖层统一口径",
    },
    {
        "claim": "LV4 之后卡住只因为 LV5 要求 min_feedback=3，界面说清「缺一次客观度量」即可",
        "verdict": "结论不完整，已补",
        "evidence": ("review_sim.py 实跑典型学习者：LV2@4→LV3@10→LV4@18 与报告一致；"
                     "卡在 LV4 时未通过项有 2 个：min_avg_quality(80/82) 与 min_feedback(2/3)。"
                     "LV5 另有 min_sessions=30、min_difficulty=4、max_idle_days=3。"),
        "action": "卡级界面显示全部未通过项；每项的 label/action 改成可执行人话（含「补一次客观度量」）",
    },
    {
        "claim": "新手第一次自报拉满 +113 = 本级 113%，修后 +23 = 23%",
        "verdict": "方向成立，绝对值不可逐位复核",
        "evidence": ("报告未附仿真脚本。按报告文字重建参数（难度5/质量90/客观度量/指标达LV5要求）"
                     "实跑得 +26 = 本级 26%，难度系数 2000 bps；与旧版 +113 的暴利已拉开量级。"),
        "action": "不改动钟形系数；把可复现脚本 review_sim.py 留在仓库，供以后逐位对照",
    },
    {
        "claim": "同一天连刷 20 次 / 分散 20 天 = 27/1134 = 0.02",
        "verdict": "方向成立，绝对值不可逐位复核",
        "evidence": ("报告未附脚本。按等价参数但任务名不重复（不触发 REPEATED_TASK 折扣）实跑"
                     " 104/497 = 0.2093；若复用同一任务名会进一步被重复折扣压低。"
                     "结论「连刷明显不划算」成立。"),
        "action": "不改动同日递减与间隔加成；把参数写进 review_sim.py",
    },
    {
        "claim": "max_idle_days 也有 ±8 小时抖动",
        "verdict": "不成立",
        "evidence": ("idle_days_bps 由 (now - last_practice_at) × 10000 // 86400 按秒连续计算"
                     "（proficiency.py::_idle_bps），完全不经过 day_index，与时区无关。"),
        "action": "不改动；在规则里写明该口径，避免下次再被误判为日历 bug",
    },
    {
        "claim": "本沙箱跑 unittest discover 会因临时目录权限 WinError 5 失败",
        "verdict": "当前环境不成立",
        "evidence": "本机 Windows 工作区实跑 python -m unittest discover 全量通过；报告结论属于其沙箱环境限制",
        "action": "本次验证一律以本机全量单测 + HTTP 联调 + 仿真脚本为准",
    },
    {
        "claim": "护道 +30 声望，攒两张卡换一次双倍",
        "verdict": "前半成立，后半表述不准确",
        "evidence": ("ALTRUISM_RULES.reputation.guard = 30；exchange_cost.insight_card = 60，"
                     "即 60 声望直接兑换 1 张悟道卡，不是「攒两张卡」；卡效果 x2 只作用一次。"),
        "action": "不改数值；本条只作核实记录",
    },
    {
        "claim": "老事件按迁移前 UTC 口径保留、新事件走本地日",
        "verdict": "不采纳",
        "evidence": ("同一账本混用两种日历口径，会让 same_day_count / 连胜 / 今日所得在切换点"
                     "对同一批事件给出互相矛盾的答案，违反状态一致性。"),
        "action": ("统一按注入偏移重算所有事件的天序号；改口径时 rebuild_streak 从只增事件日志"
                   "重建连胜，total_exp、事件日志、保持度均不变"),
    },
    {
        "claim": "9 个成就全是量；推荐永远推 metrics[0]；没有导出/导入；LV8 后等级冻结",
        "verdict": "四条均成立，已分别处理",
        "evidence": ("读代码确认：ACHIEVEMENTS 原 9 项的 metric 只有 sessions/streak/"
                     "total_xp/best_day_xp/max_level；guidance 原写死 spec=rule.metrics[0]；"
                     "web_app 无 /api/export 与 /api/import；"
                     "_LevelTable.resolve 在 level>=max_level 时 required=0。"),
        "action": ("新增 2 个质成就（personal_bests / benchmark_passes）；推荐改按最大相对缺口"
                   "并加停滞提示；新增导出/导入/备份与 /api/settings；"
                   "LV8 冻结属既定上限，本轮不动，避免改变已拍板的等级结构"),
    },
    {
        "claim": "基准复测通过可以把保持度拉回上限",
        "verdict": "不采纳",
        "evidence": ("保持度是 current_retention_bps(state, last_practice_at, now) 的纯函数，"
                     "回补会破坏可复现性与「长期搁置自然生疏」的已拍板规则。"),
        "action": "基准复测只作为证据锚与质成就来源，不写经验、不改保持度、不绕过任何判定门",
    },
)

# 叙事层：纯展示文案，不参与任何数值判定。
NARRATIVE_RULES: dict = {
    "host": "宿主",
    "daily_task_label": "今日系统任务",
    "feedback_template": "叮！宿主熟练度 +{gain}，距下一境界还需 {remaining}",
    "breakthrough_template": "境界突破 · {realm}",
    "return_hint": "系统仍在等你。上一回修行是 {days} 天前。",
    "is_local_only": True,
    "push_capability": "本地网页服务没有系统推送能力，以上仅为页面内文案，不是推送通知",
    "no_unverified_metrics": "不引用 23% 等未经一手来源证实的留存数字",
    "punishment_policy": {
        "no_hp_loss": True,
        "no_point_deduction": True,
        "no_negative_exp": True,
        "only_negative": "长期搁置导致保持度下降（自然生疏），属已拍板的现实规律，不是对迟到打卡的惩罚",
        "mitigations": [
            "1 天退化宽限期", "每周 1 次自动补签", "连胜激冻道具",
            "连胜中断降半保留不清零", "中断后恢复回补保持度",
        ],
        "note": "惩罚克制是保守设计选择，未找到支持『反生产力效应』的同行评议研究，不声称有学术背书",
    },
}

# 利他层规格：规则唯一来源，world_state.py 直接导入使用。
ALTRUISM_RULES: dict = {
    "reputation": {"guard": 30, "encourage": 5, "share": 10},
    "daily_reputation_cap": 100,
    "exchange_cost": {"insight_card": 60, "freeze_card": 40},
    "insight_multiplier_bps": 20_000,
    "base_multiplier_bps": 10_000,
    "max_cards": 9,
    "max_deeds": 5_000,
    "max_note_len": 120,
    "actors": (
        {"id": "player", "name": "宿主", "is_player": True,
         "initial_streak": 0, "day_lag": None},
        {"id": "qingya", "name": "青崖", "is_player": False,
         "initial_streak": 12, "day_lag": 1},
        {"id": "alan", "name": "阿岚", "is_player": False,
         "initial_streak": 5, "day_lag": 1},
        {"id": "yunzhou", "name": "云舟", "is_player": False,
         "initial_streak": 28, "day_lag": 2},
    ),
}

REPUTATION_TITLES: tuple[dict, ...] = (
    {"min": 0, "name": "无名行者"},
    {"min": 30, "name": "热心同修"},
    {"min": 100, "name": "护道人"},
    {"min": 300, "name": "宗门柱石"},
    {"min": 800, "name": "一方善士"},
)

# 利他层判定原因码（与 world_state.evaluate_deed / WorldState 写入返回值一致）
ALTRUISM_REASON_CODES: dict[str, str] = {
    "OK": "已计入声望",
    "UNKNOWN_DEED": "没有这种利他行为",
    "UNKNOWN_ACTOR": "档案不存在",
    "UNKNOWN_TARGET": "没有这位同门",
    "TARGET_NOT_AT_RISK": "这位同门现在没有断签风险",
    "UNKNOWN_EVENT": "这条修行记录不存在",
    "EVENT_NOT_PRACTICE": "只有有效练习能公开为范例",
    "DUPLICATE": "这件事已经做过了，不重复计声望",
    "DAILY_CAP": "今日声望已达上限，明天再来",
    "NOT_ENOUGH_REPUTATION": "声望还不够",
    "CARD_LIMIT": "这类卡已达持有上限",
    "NO_CARD": "手里没有这张卡",
    "ALREADY_ARMED": "悟道卡已装备，先完成一次练习",
    "UNKNOWN_ITEM": "没有这个兑换项",
    "NOT_ARMED": "没有待用的悟道效果",
    "FREEZE_GRANTED": "连胜激冻已装备，断签时自动生效",
}

# 医学安全门常量（提前定义，供 REASON_CODES 文案引用）
RPE_MIN, RPE_MAX = 6, 20       # Borg RPE 6–20 量表范围
RPE_VIGOROUS = 14              # ACSM：≥14 属剧烈强度
RPE_REJECT_ABOVE = 17          # 超过 17 属极高强度，不计入有效练习

# 实际反馈：等级用于「可观测达标条件」，系数用于单次经验
FEEDBACK_LEVELS: dict[str, int] = {
    "none": 0,        # 无反馈，做完不知道对错
    "self": 1,        # 自我检查 / 对照标准答案
    "external": 2,    # 他人点评 / 教练指导
    "measured": 3,    # 客观度量（计时、评分表、仪器、考试）
}
FEEDBACK_FACTOR_BPS: dict[str, int] = {
    "none": 5_000, "self": 8_000, "external": 10_000, "measured": 12_500,
}

# 难度匹配：任务难度相对当前等级带的落差
CHALLENGE_FACTOR_BPS: dict[str, int] = {
    "stretch": 10_000,   # 难度 >= 当前难度带：有效挑战
    "maintain": 5_000,   # 难度 = 当前难度带 - 1：维持性练习
    "low": 1_500,        # 难度 <= 当前难度带 - 2：低难度重复
}
REPEAT_FACTOR_BPS = 2_000   # 同任务短期内重复，收益打 2 折

MIN_QUALITY = 40                 # 质量低于 40 不构成有效练习
NO_FEEDBACK_QUALITY_GATE = 70   # 无反馈时质量须 >= 70 才算有效
LOW_STREAK_LIMIT = 3            # 连续第 3 次低难度练习起判无效
REPEAT_WINDOW = 3               # 最近 3 次练习的指纹窗口

# 用户没填任务标识时，服务层写入这些占位名。它们不能被当作「同一个任务」，
# 否则最省事的用户会从第 3 次练习起被判 REPEATED_TASK 永久无效。
_UNNAMED_TASK_KEYS = frozenset({"", "未命名", "未命名任务"})
RESUME_IDLE_DAYS = 14           # 搁置 >= 14 天记为「中断后恢复」
DECAY_GRACE_SECONDS = DAY_SECONDS  # 距上次有效练习不足 1 天，不触发等级退化

# 退化宽限期的作用：1 天内保持度只衰减约 0.5%，若有效熟练度恰卡在
# 等级阈值上，会来回跳级并产生虚假的升级/退化事件。宽限期内保持度与
# 百分比照常变化，但等级不降；>= 1 天才按有效熟练度判定退化。

# 生疏锚点：(搁置天数, 该时点保持度上限 bps)。两点之间线性插值，全整数运算。
# 0 天不衰减；1 天内衰减极慢；之后加速；365 天后收敛到下限。
DECAY_ANCHORS: tuple[tuple[int, int], ...] = (
    (0, 10_000), (1, 10_000), (3, 9_800), (7, 9_500), (14, 8_900),
    (30, 7_800), (60, 6_500), (120, 5_000), (365, 3_500),
)
RETENTION_FLOOR_BPS = 3_500

# 无效练习原因码（判定顺序即下列顺序，先命中先返回）
REASON_CODES: dict[str, str] = {
    "OK": "有效练习，计入熟练度",
    "TIME_REVERSED": "结算时间不晚于上次有效练习，拒绝（防止重复结算与时间倒流）",
    "QUALITY_BELOW_MIN": f"完成质量低于 {MIN_QUALITY}，视为无效练习",
    "NO_FEEDBACK_LOW_QUALITY": f"无实际反馈且质量低于 {NO_FEEDBACK_QUALITY_GATE}，视为无效练习",
    "LOW_DIFFICULTY_STREAK": f"连续第 {LOW_STREAK_LIMIT} 次及以上低难度重复，视为无效练习",
    "REPEATED_TASK": f"最近 {REPEAT_WINDOW} 次练习均为同一任务，视为机械重复、无效",
    "INVALID_INPUT": "输入不合法（难度/质量/反馈/时间）",
    "MISSING_METRICS": "缺少该技能专属的可观测指标，或包含未定义的指标字段",
    "INVALID_METRICS": "指标值不是整数或超出该指标声明的合法范围",
    "PAIN_REPORTED": "本次练习记录了疼痛/不适，按医学安全门不计入熟练度",
    "MEDICAL_STOP_SIGN": "出现停止运动警示信号（胸痛/头晕/异常气促/关节痛加重等），不计入并建议停止",
    "INVALID_MEDICAL": "医学安全门字段缺失、非整数或 RPE 超出 Borg 6–20 范围",
    "RPE_TOO_HIGH": f"RPE > {RPE_REJECT_ABOVE} 属极高强度，按安全门不计入有效练习",
    "SKILL_GATE_FAILED": "指标未达到该技能「有效练习」的最低门槛，本次不计入熟练度",
}

# LV1–LV8 统一画像，对齐 EQF 8 级「知识/技能/责任与自主」描述符。
# observable 是对 EQF 官方描述符的概括，来源见 STANDARD_SOURCES["eqf"]。
# min_sessions / min_avg_quality / min_feedback / min_difficulty / max_idle_days
# 是本系统的可配置默认标尺，不是 EQF 或任何外部标准的规定值。
# min_effective_exp 由成长规则阈值表派生，不在本表硬编码（见 rulebook()）。
LEVEL_CRITERIA: tuple[dict, ...] = (
    {
        "level": 1, "name": "入门", "eqf": 1, "min_sessions": 0, "min_avg_quality": 0,
        "min_feedback": 0, "min_difficulty": 1, "max_idle_days": None,
        "observable": "具备基础常识，能在直接监督、结构化情境下完成简单任务。",
    },
    {
        "level": 2, "name": "基础", "eqf": 2, "min_sessions": 3, "min_avg_quality": 50,
        "min_feedback": 1, "min_difficulty": 1, "max_idle_days": 30,
        "observable": "具备基础事实性知识，能用简单规则与工具解决常规问题，在监督下有一定自主性。",
    },
    {
        "level": 3, "name": "独立执行", "eqf": 3, "min_sessions": 8, "min_avg_quality": 65,
        "min_feedback": 2, "min_difficulty": 2, "max_idle_days": 14,
        "observable": "理解事实、原理与过程，能选择并应用基本方法完成任务，对任务完成负责并随情境调整行为。",
    },
    {
        "level": 4, "name": "自主", "eqf": 4, "min_sessions": 16, "min_avg_quality": 75,
        "min_feedback": 2, "min_difficulty": 3, "max_idle_days": 7,
        "observable": "在广泛情境中生成具体问题的解决方案，能自我管理并监督他人的常规工作。",
    },
    {
        "level": 5, "name": "专精", "eqf": 5, "min_sessions": 30, "min_avg_quality": 82,
        "min_feedback": 3, "min_difficulty": 4, "max_idle_days": 3,
        "observable": "具备全面且专门化的知识并知道其边界，能对抽象问题提出创造性方案，在不可预测变化中管理并复盘自己与他人的表现。",
    },
    {
        "level": 6, "name": "高级", "eqf": 6, "min_sessions": 50, "min_avg_quality": 88,
        "min_feedback": 3, "min_difficulty": 5, "max_idle_days": 1,
        "observable": "对理论与原理有批判性理解，能解决专业领域复杂不可预测问题，管理复杂项目并承担决策责任。",
    },
    {
        "level": 7, "name": "前沿", "eqf": 7, "min_sessions": 80, "min_avg_quality": 90,
        "min_feedback": 3, "min_difficulty": 5, "max_idle_days": 1,
        "observable": "掌握领域前沿高度专门化知识，能在研究或创新中开发新知识与程序，管理并转型复杂情境、贡献专业实践。",
    },
    {
        "level": 8, "name": "权威", "eqf": 8, "min_sessions": 120, "min_avg_quality": 92,
        "min_feedback": 3, "min_difficulty": 5, "max_idle_days": 1,
        "observable": "处于领域最前沿并能综合与评估，以扩展或重新定义既有知识与专业实践；展现出实质权威、创新与学术/专业诚信。",
    },
)

MAX_LEVEL = len(LEVEL_CRITERIA)

# --------------------------------------------------------------------------
# 权威标准出处：全部来自本次联网实抓，逐条标注一手/二次来源
# --------------------------------------------------------------------------
# grade: "primary" = 标准发布方原文/官方页面；"secondary" = 对标准的第三方解读
# kind:  language / sport / medical / computer / framework
# 只有 primary 可直接作为「硬标准」；secondary 仅用于解释，不单独作为阈值依据。
STANDARD_SOURCES: dict[str, dict] = {
    "workbuddy_template": {
        "name": "【WorkBuddy】技能等级LV1-8框架模板.md",
        "url": "https://www.workbuddy.cn/space/d/9xy0YVi3YaFHLctkhIWGtf?shareId=a7221681-1ac5-4c73-951c-2ccd40a53aa2",
        "grade": "primary", "kind": "framework",
        "note": "本次按用户指定读取。文中 2.1–2.7 七领域锚点表为待填写模板（无具体阈值）；第三节给出了跑步/健身/剪辑的示例草案并明确标注「⚠️ 需你拍板」。",
    },
    "cefr": {
        "name": "CEFR 欧洲语言共同参考框架（Council of Europe / Europass）",
        "url": "https://europa.eu/europass/en/common-european-framework-reference",
        "grade": "primary", "kind": "language",
        "note": "A1/A2 基础用户、B1/B2 独立用户、C1/C2 熟练用户；官方 can-do 描述覆盖听/说/读/写。",
    },
    "cefr_pdf": {
        "name": "CEFR 官方文件（Council of Europe, EC 镜像）",
        "url": "https://ec.europa.eu/oil/doc/AO/2012/09-2012/09-2012-cadre_langues-en-v00.pdf",
        "grade": "primary", "kind": "language",
        "note": "C2/C1/B2/B1/A2/A1 六级 global descriptor 原文。",
    },
    "dele": {
        "name": "DELE 对外西班牙语水平证书（塞万提斯学院 / 中国教育考试网）",
        "url": "https://dele.neea.edu.cn/",
        "grade": "primary", "kind": "language",
        "note": "官方报名页确认考试级别为 A1–C2；DELE 严格对应 CEFR 六级的说明见二次来源 spanish_dele_map。",
    },
    "spanish_dele_map": {
        "name": "DELE A1–C2 与 CEFR 对应说明（西班牙文化协会，二次来源）",
        "url": "https://www.spanish.hk/post/香港-dele-西班牙文考試全攻略",
        "grade": "secondary", "kind": "language",
        "note": "逐级别能力摘要；DELE 证书终身有效。仅作解释，不单独作为阈值依据。",
    },
    "eqf": {
        "name": "EQF 欧洲资格框架 8 级描述符（欧盟官方期刊 / Europass）",
        "url": "https://europa.eu/europass/en/european-qualifications-framework-eqf",
        "grade": "primary", "kind": "framework",
        "note": "8 级按知识/技能/责任与自主三维描述；2008 设立、2017 修订。LV1–LV8 名称与画像据此概括。",
    },
    "eqf_descriptors": {
        "name": "EQF 8 级描述符原文（EUR-Lex 欧盟官方公报）",
        "url": "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=OJ:C:2008:111:FULL",
        "grade": "primary", "kind": "framework",
        "note": "Level 1–8 逐条学习成果原文。",
    },
    "acsm_running": {
        "name": "ACSM《Healthy Habits for Distance Running》",
        "url": "https://acsm.org/?p=12296",
        "grade": "primary", "kind": "sport",
        "note": "步频较习惯值提高 5–10% 通常使步频 >170 步/分，可带来降低损伤风险的机械效益；ACSM 明确说明不存在适用于所有跑者的「gold standard」重训方案。",
    },
    "acsm_rt2009": {
        "name": "ACSM Position Stand: Progression Models in Resistance Training (2009, PubMed)",
        "url": "https://pubmed.ncbi.nlm.nih.gov/19204579/",
        "grade": "primary", "kind": "sport",
        "note": "新手 8–12RM；中级约 6 个月规律训练；高级 1–12RM 周期化、强调 1–6RM；频率新手 2–3 天/周、中级 3–4、高级 4–5；能多做 1–2 次时加 2–10% 负荷。",
    },
    "acsm_rt2026": {
        "name": "ACSM 2026 Resistance Training Position Stand",
        "url": "https://acsm.org/resistance-training-guidelines",
        "grade": "primary", "kind": "sport",
        "note": "137 篇系统综述、3 万+ 受试者；主要收益来自「从无到有」的坚持，主要肌群每周至少 2 次；力量 80% 1RM × 2–3 组；增肌约 10 组/肌群/周；强调个体化，反对一刀切。",
    },
    "borg_rpe": {
        "name": "Borg RPE 6–20 主观用力等级（Borg 1998；ACSM 强度分带）",
        "url": "https://www.hospitaldaluz.pt/arrabida/en/health-and-wellness/perceived-exertion",
        "grade": "secondary", "kind": "medical",
        "note": "6=毫不费力、20=极限；12–14 为中等强度。ACSM 11–14 为常用运动区间；≥14 属剧烈强度。原始出处 Borg G. Perceived Exertion and Pain Scales, 1998。",
    },
    "acsm_screening": {
        "name": "ACSM 运动前筛查算法与医学许可（二次解读）",
        "url": "https://exampreprise.com/study-guides/acsm-ep/screening-algorithm",
        "grade": "secondary", "kind": "medical",
        "note": "任何有症状者开始/继续运动前均需医学许可；RPE <14 约为轻中等强度；出现新发胸痛、头晕、异常气促须立即停止并重新筛查。",
    },
    "stop_signs": {
        "name": "停止运动警示信号（ACSM/ACS 汇编，二次来源）",
        "url": "https://www.lifewithcancer.org/wp-content/uploads/2020/03/CHAIR-IN-THE-MEANTIME-EX-Suggestions.pdf",
        "grade": "secondary", "kind": "medical",
        "note": "胸痛/压迫感、心悸、头晕、异常气促、关节疼痛随活动加重等须停止；中等强度建议 RPE 11–14。",
    },
    "mos": {
        "name": "Microsoft Office Specialist / ICDL 认证阶梯（认证路径汇总）",
        "url": "https://getting-digital.net/office",
        "grade": "secondary", "kind": "computer",
        "note": "MOS 按单个应用考试（如 MO-210 Excel Associate → MO-211 Excel Expert）；ICDL 模块化覆盖基础数字素养。Associate 需 3 门不同应用、Expert 需先持 Associate 再 2 门 Expert（见 mos_pathway）。",
    },
    "mos_pathway": {
        "name": "MOS Associate / Expert 路径要求（Certiport 授权中心汇总）",
        "url": "https://www.edukinect.com/pages/certification-pathways",
        "grade": "secondary", "kind": "computer",
        "note": "Associate：任意 3 门不同应用且至少 1 门 M365；Expert：先取得 Associate，再考 2 门不同应用的 Expert。",
    },
    "adobe_acp": {
        "name": "Adobe Certified Professional — Premiere Pro 官方考试指南",
        "url": "https://certifiedprofessional.adobe.com/premiere",
        "grade": "primary", "kind": "computer",
        "note": "官方明确为 entry-level 入门级认证；目标考生约 150 小时教学与实操经验；50 分钟考试，含应用内实操；考查 5 大目标域（行业协作/项目设置/组织项目/创建修改视觉元素/发布数字媒体）。",
    },
    "adobe_acp_cn": {
        "name": "Adobe 国际认证 Premiere Pro 认证专家（中文官方页）",
        "url": "https://www.adobeacp.com/h5/app/subject-pr.html",
        "grade": "primary", "kind": "computer",
        "note": "确认 150 小时学习与实践经验、50 分钟真实应用环境考试，以及无字幕/版权/导出等考纲条目。",
    },
}

# 文章明确标注「⚠️ 需你拍板」的项目 + 系统层决策。
# 每项给 2–3 个合理选项、默认推荐与影响说明；当前实现采用 recommended。
# 状态：全部已定稿（decided）。selected 是最终采用的选项，必须与真实实现一致；
# 复核时发现 5 项原先的推荐只是「说了但没做」，已改成代码里真正成立的做法。
DECISION_POINTS: tuple[dict, ...] = (
    {
        "id": "level_scale", "group": "框架", "question": "等级刻度用几级？", "status": "decided",
        "options": [
            {"key": "eqf8", "label": "统一 LV1–LV8，对齐 EQF 8 级", "effect": "与文章/EQF 一致；但语言类只有 6 个官方等级"},
            {"key": "cefr6", "label": "统一 6 级，贴合 CEFR 自然六级", "effect": "语言最贴合；运动与计算机技能的高阶层次被压缩"},
            {"key": "hybrid", "label": "框架 8 级，但每个技能声明官方上限", "effect": "超出的等级标注「无官方对应，待定」"},
        ],
        "selected": "hybrid",
        "rationale": "尺子统一做成 8 格，方便跟学历、职业资格对上；但语言官方只到 6 格，剩下两格写『还没官方说法』，不自己瞎填。",
        "note": "文章：『每个领域不必填满 8 级，超出的 LV 标注「无官方对应，待定」』。",
    },
    {
        "id": "language_lv78", "group": "语言", "question": "英语/西语 LV7–LV8 怎么定？", "status": "decided",
        "options": [
            {"key": "leave_blank", "label": "留空，标注无官方对应", "effect": "最严谨；CEFR C2 已是最高级"},
            {"key": "cse", "label": "对接中国英语能力等级量表 CSE 高阶", "effect": "可覆盖更高阶，但 CSE↔CEFR 对接关系未取得一手核定"},
            {"key": "professional", "label": "自定义「专业领域/近母语」两级", "effect": "属于自建标准，须标注非官方"},
        ],
        "selected": "leave_blank",
        "rationale": "CEFR 最高就是 C2，已经到顶；再往上编两级就是自己造标准，不如空着。",
        "note": "文章：『语言/办公四类直接套硬标准，不要重造阈值』。",
    },
    {
        "id": "run_pace", "group": "跑步", "question": "各级配速阈值与级差？", "status": "decided",
        "options": [
            {"key": "article_draft", "label": "采用文章示例草案（5km≤8:00 → 全马≤6:00/km）", "effect": "文章明确标注为非官方示例"},
            {"key": "no_pace", "label": "不设配速硬门槛，只用距离 + ACSM 步频 + RPE", "effect": "阈值都有运动科学出处"},
            {"key": "ten_percent", "label": "以「周跑量增幅 ≤10%」为进阶门，配速仅作展示", "effect": "需要长期数据，当前未实现"},
        ],
        "selected": "no_pace",
        "rationale": "配速那几个数是文章按跑圈常识推的，不是官方标准。系统只认有出处的：跑了多远、步频、累不累。",
        "note": "文章：配速阈值『基于跑圈常识推导，非官方标准』，公认硬门槛仅「全马破 4」。",
    },
    {
        "id": "run_volume", "group": "跑步", "question": "月跑量是否作为硬门槛？", "status": "decided",
        "options": [
            {"key": "none", "label": "不纳入", "effect": "与当前实现一致；避免用堆量替代技术"},
            {"key": "auxiliary", "label": "作辅助指标只展示不否决", "effect": "需要新增指标与界面，当前未实现"},
            {"key": "hard", "label": "硬门槛（如 LV5 ≥80 km/月）", "effect": "80 是草案值，无官方依据"},
        ],
        "selected": "none",
        "rationale": "系统里本来就没有月跑量这一项，说『辅助展示』等于空头承诺。ACSM 2026 也强调关键是坚持，不是堆量。",
        "note": "复核修正：原推荐 auxiliary 与实现不符，改判 none。",
    },
    {
        "id": "strength_metric", "group": "健身", "question": "力量标尺用哪个？", "status": "decided",
        "options": [
            {"key": "relative", "label": "体重倍数（深蹲/硬拉 1RM ÷ 体重）", "effect": "文章草案采用，但倍数阈值非官方"},
            {"key": "absolute", "label": "绝对重量（kg）", "effect": "直观，但对轻体重者不公平"},
            {"key": "acsm_rm", "label": "按 ACSM 的 RM/频率/递进规则，不设倍数阈值", "effect": "全部有 Position Stand 出处"},
        ],
        "selected": "acsm_rm",
        "rationale": "体重倍数那些数字文章自己标了『非官方』。ACSM 写了新手一组做 8–12 次、每周 2–3 回、能多做两次就加重量，这些有出处。",
        "note": "文章：『倍数阈值为示例，非官方标准』。",
    },
    {
        "id": "strength_judge", "group": "健身", "question": "动作标准由谁鉴定？", "status": "decided",
        "options": [
            {"key": "self", "label": "自评（当前实现）", "effect": "零成本，主观性最高；与数据来源自报一致"},
            {"key": "video", "label": "视频审核（教练或同伴复核）", "effect": "需上传与审核流程，当前未实现"},
            {"key": "onsite", "label": "线下考核/认证教练签字", "effect": "最可信，成本最高，当前未实现"},
        ],
        "selected": "self",
        "rationale": "系统是填数字自报，没有传视频的功能，定成视频复核就是说了做不到。动作分明确按自评处理。",
        "note": "复核修正：原推荐 video 与数据来源自报冲突且未实现，改判 self。",
    },
    {
        "id": "edit_acp", "group": "剪辑", "question": "ACP 认证怎么参与？", "status": "decided",
        "options": [
            {"key": "hard", "label": "硬认证：持证才允许判到 LV2", "effect": "会卡住自学路径"},
            {"key": "bonus", "label": "作为加分项", "effect": "加减熟练度没有官方依据"},
            {"key": "none", "label": "不参与计分，仅作为 LV1–LV2 等级描述（当前实现）", "effect": "证书用来说明该级该会什么"},
        ],
        "selected": "none",
        "rationale": "ACP 是入门级证书，适合说明『一、二级该会什么』。拿它给熟练度加减分没有依据，所以只写在等级说明里。",
        "note": "复核修正：原推荐 bonus 未实现，改判 none（仅描述）。",
    },
    {
        "id": "edit_track", "group": "剪辑", "question": "是否区分横屏影视与短视频两条晋升线？", "status": "decided",
        "options": [
            {"key": "single", "label": "单一晋升线", "effect": "简单清晰"},
            {"key": "dual", "label": "两条独立晋升线", "effect": "技能状态与界面翻倍"},
        ],
        "selected": "single",
        "rationale": "两条线要把所有状态和界面翻一倍，普通人更容易看晕，先用一条讲清楚。",
    },
    {
        "id": "cert_vs_exp", "group": "系统", "question": "证书与熟练度经验的关系？", "status": "decided",
        "options": [
            {"key": "hard_gate", "label": "证书作升级硬门槛", "effect": "没证书就无法升级"},
            {"key": "evidence", "label": "单轨：经验照常攒，证书作为达标校验的证据项（当前实现）", "effect": "办公技能的 cert_level 指标即此用途"},
            {"key": "dual_track", "label": "经验等级与认证等级双轨并列", "effect": "需新增状态与界面，当前未实现"},
        ],
        "selected": "evidence",
        "rationale": "系统只算一条熟练度轨。证书信息当作『这一级算不算真达标』的证据之一，不另开一条轨，免得两个等级打架。",
        "note": "复核修正：原推荐 dual_track 未实现，改判 evidence。",
    },
    {
        "id": "decay_scope", "group": "系统", "question": "生疏退化影响哪些状态？", "status": "decided",
        "options": [
            {"key": "exp_only", "label": "只作用于有效熟练度；认证信息不参与退化（当前实现）", "effect": "符合现实：证书不过期，手会生"},
            {"key": "both", "label": "两者同步退化", "effect": "与现实不符，且无认证轨可同步"},
            {"key": "none", "label": "关闭退化", "effect": "失去「长期搁置生疏」机制"},
        ],
        "selected": "exp_only",
        "rationale": "证书不会过期，手会生。系统会生疏、会掉级，但只掉熟练度这一条轨。",
        "note": "系统未维护独立认证等级轨，故 ⑩ 与 ⑨ 保持一致：只谈熟练度。",
    },
    {
        "id": "data_source", "group": "系统", "question": "指标数据从哪来？", "status": "decided",
        "options": [
            {"key": "self_report", "label": "使用者自报（当前实现）", "effect": "零成本，依赖自觉"},
            {"key": "import", "label": "证书/成绩/设备数据导入并校验", "effect": "最准确，需对接外部数据源"},
            {"key": "review", "label": "自报 + 第三方复核后才计入", "effect": "折中，增加审核流程"},
        ],
        "selected": "self_report",
        "rationale": "零门槛，打开就能用。代价是靠自觉，所以要求指标填全、填整数、在合理范围内，填错就不算。",
    },
)

# --------------------------------------------------------------------------
# 大白话版提升规则：给不看代码的人看，每条都标注对应的实现位置，便于核对
# --------------------------------------------------------------------------
PLAIN_RULES: tuple[dict, ...] = (
    {
        "title": "1. 练了不一定算数，先把这次的数字填对",
        "text": "每练一次，要把这个技能要求填的几个数字填全。少填一个、多填一个、"
                "写成小数、或者填了个离谱的数，这一次就白填，一点熟练度都不加，也不会留下记录。",
        "tech": "evaluate_skill_metrics → MISSING_METRICS / INVALID_METRICS；不通过时状态零改动",
    },
    {
        "title": "2. 加多少，看四件事",
        "text": "活儿有多难（1 到 5 分）、这次做得好不好（0 到 100 分）、"
                "有没有人给你反馈（没人看 / 自己对答案 / 别人点评 / 用成绩或仪器量）、"
                "以及是不是老在重复同一件事。越难、做得越好、反馈越实在，加得越多；"
                "重复同一件事会大打折扣。",
        "tech": "gain = round(20×难度 × 质量/100 × 反馈系数 × 难度匹配系数 × 重复系数)",
    },
    {
        "title": "3. 每个技能有自己的最低线",
        "text": "比如跑步要填跑了多远、一分钟迈多少步、动作自己打几分；"
                "学英语要填说了多久、用了几个新词、对方听懂多少。"
                "其中任何一个没到这个技能的最低线，这次就不算有效练习。",
        "tech": "SKILL_RULES 每个技能的 3 个 MetricSpec.min_valid → SKILL_GATE_FAILED",
    },
    {
        "title": "4. 身体不舒服，这次直接不算，还提醒你该停",
        "text": "跑步和健身要多填三个数：有没有疼痛、这次有多累（6 到 20 的量表）、"
                "有没有出现胸口疼、头晕、喘不上气这类信号。"
                "只要报了疼、报了信号，或者累到 17 以上，这次就不计入熟练度。",
        "tech": "MEDICAL_KEYS + Borg RPE 6–20 / ACSM 停止信号 → PAIN_REPORTED / MEDICAL_STOP_SIGN / RPE_TOO_HIGH",
    },
    {
        "title": "5. 做得特别好，额外多给一点",
        "text": "填的数字里，每有一项达到这个技能的「优秀线」，这次就多给 5%。"
                "三项都特别好，就多给 15%。",
        "tech": "S = 10000 + 500 × 达到 good 线的指标数（bps）",
    },
    {
        "title": "6. 刚开始升得快，越往后越慢",
        "text": "从 1 级升到 2 级只要攒够 100 点，2 升 3 要 283 点，3 升 4 要 520 点，"
                "再往后要 800、1119……所以新手几天连升几级，老手练很久才挪一格。"
                "这不是系统卡你，是「入门容易精通难」。",
        "tech": "PowerRule: cost(L) = ceil(base × L^1.5)",
    },
    {
        "title": "7. 长时间不练，会生疏，等级会往回掉",
        "text": "一天之内不练不掉。超过一天没练，「保持度」就慢慢往下走："
                "一周剩 95%，一个月剩 78%，四个月剩一半，一年以上最低剩 35%。"
                "保持度一低，实际算数的熟练度就变少，等级可能往回退。",
        "tech": "DECAY_ANCHORS 分段线性插值；退化由 settle(now) 显式触发，不隐式发生",
    },
    {
        "title": "8. 退了不等于白学，捡回来比重新学快",
        "text": "以前练出来的总量一直留着，不清零。搁久了再练一次，保持度会往回补，"
                "等级很快就能升回来，比从头开始快得多。",
        "tech": "total_exp 单调不减；recovery_bps 回补保持度；resumed 标记中断后恢复",
    },
    {
        "title": "9. 经验攒够了，也不一定让你升级",
        "text": "每一级都写了一句人话，比如「能不看稿连续讲 8 分钟」「能独立跑完 5 公里」。"
                "按你拍板的结果，光把点数攒够没用：系统会拿你历次练习的平均水平逐条核对，"
                "练习次数、平均质量、反馈强度、难度、指标均值都得过线才放你升级。"
                "没过线就停在原级，进度条显示满格，旁边直接告诉你还差哪几项。",
        "tech": "assess_gate=True：_granted_level 逐级调用 assess_skill_level；gate_blocked / exp_level / next_level_criteria",
    },
    {
        "title": "10. 官方没有的等级，要么留白，要么写明是自己定的",
        "text": "语言考级最高到 C2，办公软件认证到 Expert，剪辑的官方证书只是入门级。"
                "按你拍板的结果，语言的 7、8 级和剪辑的 3 到 8 级由系统自己补了标准，"
                "但每一条都明写「自建标准·非官方」，不冒充官方分级；办公软件的 6 到 8 级"
                "仍然老老实实写「无官方对应，待定」。网页上每个数字都标了它的来源。",
        "tech": "official_max_level / self_defined_levels / mapping_kind(official|self_defined|unmapped) / MetricSpec.basis",
    },
    {
        "title": "11. 每条记录可以附一句「证明声明」，但现在不验真",
        "text": "每条修行记录都留了两个空位：证明类型和证明链接。你可以声明「这是当场拍的」"
                "或「同伴看过了」，但本机不会收图、不会看图、也不会判断真假，"
                "只是把这句话记下来，等以后接了真正的核验服务还能补验历史。"
                "所以现在所有完成依然是自己报的，数字含金量取决于你填得准不准。",
        "tech": "PracticeEvent.proof_type / proof_url；TRUST_RULES.verification_implemented=False",
    },
    {
        "title": "12. 帮别人，也能换成自己的一点助力",
        "text": "江湖页里有几位同门（本机模拟，不是真人）。他们的连胜快断时你可以「护道」，"
                "给真实的一条修行记录留句鼓励、或把自己的完成记录公开当范例，都能攒「声望」。"
                "声望只能靠帮人得，不能买；攒够了能换「悟道卡」（下一次有效练习翻倍）"
                "或「连胜激冻」（断签时保一次连胜）。系统不扣血、不扣分、不倒扣熟练度，"
                "唯一会往下走的是长期不练的自然生疏，而且有宽限。",
        "tech": "WorldState guard/encourage/share/exchange/use_insight/use_freeze；ALTRUISM_RULES；NARRATIVE_RULES.punishment_policy",
    },
    {
        "title": "13. 按标准练会升级，突击不再和天天练一样",
        "text": "以前算的是「从第一次到现在的平均分」，起步低的人练一辈子也差那么一点。现在只看最近 10 次、"
                "去掉最差 2 次；连续 3 次每项都达标，还能直接放行。同一天反复刷，第 2 次只剩六成、"
                "第 5 次不到三成；隔一两天再来反而多给一点。填「特别难又特别完美」也不再划算："
                "难度超出本级太多，收益会明显打折。系统不扣分、不扣血，只是把「天天练、诚实填」算得更值。",
        "tech": "SHAPE_RULES 近期窗口截尾均值 / quick_pass / 钟形难度 / 同日递减 / 间隔加成；shape_enabled=False 可回退",
    },
)


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PracticeRecord:
    """一次练习的可观测输入。now 必须是显式 Unix 秒整数。"""
    difficulty: int          # 1..5
    quality: int             # 0..100
    feedback: str            # none / self / external / measured
    task_key: str            # 任务标识，用于机械重复判定
    now: int                 # Unix 秒
    note: str = ""


@dataclass(frozen=True)
class PracticeContext:
    """结算时从技能状态读出的上下文，全部显式传入。"""
    level: int
    retention_bps: int
    last_practice_at: int
    recent_fingerprints: tuple[str, ...]
    low_streak: int
    # 本次是该技能「同一 UTC 自然日」内的第几次有效练习（1 起）。
    # 0 或 1 都表示首次，不触发同日递减；由服务层从事件日志显式算出。
    same_day_count: int = 0


@dataclass(frozen=True)
class PracticeVerdict:
    valid: bool
    reason_code: str
    gain: int                       # 本次有效经验，无效时为 0
    factors: dict = field(default_factory=dict)
    challenge: str = ""             # stretch / maintain / low
    idle_days_bps: int = 0          # 搁置天数 x 10000，整数避免浮点
    resumed: bool = False           # 是否为中断后恢复
    recovery_bps: int = 0           # 本次回补的保持度
    skill_factor_bps: int = 10_000  # 技能专属指标加成系数
    metric_checks: tuple[dict, ...] = ()  # 每个指标的实测值/门槛/是否通过


# --------------------------------------------------------------------------
# 纯函数判定
# --------------------------------------------------------------------------

def level_band(level: int) -> int:
    """技能等级映射到任务难度带 1..5。"""
    return min(5, max(1, level))


def classify_challenge(difficulty: int, level: int) -> str:
    band = level_band(level)
    if difficulty >= band:
        return "stretch"
    if difficulty == band - 1:
        return "maintain"
    return "low"


def retention_cap_bps(idle_seconds: int) -> int:
    """搁置时长 -> 保持度上限（0..10000 bps）。分段线性插值，全整数。

    idle_seconds 先换算成「天的万分之一」再插值，避免任何浮点参与。
    """
    if idle_seconds <= 0 or idle_seconds < DECAY_GRACE_SECONDS:
        return 10_000  # 退化宽限期：1 天内不生疏，等级不会在阈值边上来回跳
    x = idle_seconds * 10_000 // DAY_SECONDS
    last_x = DECAY_ANCHORS[-1][0] * 10_000
    if x >= last_x:
        return RETENTION_FLOOR_BPS
    for (x0, y0), (x1, y1) in zip(DECAY_ANCHORS, DECAY_ANCHORS[1:]):
        ax0, ax1 = x0 * 10_000, x1 * 10_000
        if x <= ax1:
            if x <= ax0:
                return y0
            return y0 + (y1 - y0) * (x - ax0) // (ax1 - ax0)
    return RETENTION_FLOOR_BPS


def current_retention_bps(stored_bps: int, last_practice_at: int, now: int) -> int:
    """当前保持度 = 上次结算后的保持度 与 时间衰减上限 取较小值。"""
    if last_practice_at <= 0:
        return max(0, min(10_000, stored_bps))
    cap = retention_cap_bps(max(0, now - last_practice_at))
    return max(0, min(10_000, stored_bps, cap))


def effective_exp(total_exp: int, retention_bps: int) -> int:
    return max(0, total_exp) * max(0, min(10_000, retention_bps)) // 10_000


def recovery_bps(quality: int, challenge: str, idle_days_bps: int) -> int:
    """一次有效练习回补的保持度（bps）。中断越久，首次回补略多。"""
    value = 400 + 6 * quality + (100 if challenge == "stretch" else 0)
    if idle_days_bps >= RESUME_IDLE_DAYS * 10_000:
        value += 200
    return max(0, min(10_000, value))


def evaluate_practice(record: PracticeRecord, ctx: PracticeContext,
                      *, shape_enabled: bool = False) -> PracticeVerdict:
    """有效练习判定器：给定记录与上下文，返回唯一确定的判定结果。

    判定顺序（先命中先返回，顺序即规则的一部分）：
      1 输入合法性      2 时间不倒流      3 质量门槛
      4 无反馈+低质量   5 连续低难度      6 机械重复任务      7 计分

    shape_enabled=False（纯函数默认）保持 2026-11-08 之前的计分口径，
    供等级引擎契约测试与对照仿真使用；系统层由 ProficiencySystem 按
    SHAPE_RULES["enabled"] 显式开启钟形难度 / 同日递减 / 间隔加成。
    """
    idle_seconds = max(0, record.now - ctx.last_practice_at) if ctx.last_practice_at > 0 else 0
    idle_days_bps = idle_seconds * 10_000 // DAY_SECONDS
    base = {"idle_days_bps": idle_days_bps,
            "resumed": idle_days_bps >= RESUME_IDLE_DAYS * 10_000}

    # 1 输入合法性
    if (
        isinstance(record.difficulty, bool) or isinstance(record.quality, bool)
        or isinstance(record.now, bool)
        or not isinstance(record.difficulty, int) or not isinstance(record.quality, int)
        or not isinstance(record.now, int)
        or not (1 <= record.difficulty <= 5)
        or not (0 <= record.quality <= 100)
        or record.feedback not in FEEDBACK_LEVELS
        or not isinstance(record.task_key, str) or not record.task_key
    ):
        return PracticeVerdict(False, "INVALID_INPUT", 0, base)

    # 2 时间不倒流（首次练习 last_practice_at=0，不受此约束）
    if ctx.last_practice_at > 0 and record.now <= ctx.last_practice_at:
        return PracticeVerdict(False, "TIME_REVERSED", 0, base)

    # 3 质量门槛
    if record.quality < MIN_QUALITY:
        return PracticeVerdict(False, "QUALITY_BELOW_MIN", 0, base)

    # 4 无反馈且质量不高
    if record.feedback == "none" and record.quality < NO_FEEDBACK_QUALITY_GATE:
        return PracticeVerdict(False, "NO_FEEDBACK_LOW_QUALITY", 0, base)

    challenge = classify_challenge(record.difficulty, ctx.level)
    window = list(ctx.recent_fingerprints[-REPEAT_WINDOW:])

    # 5 连续低难度：本次是第 LOW_STREAK_LIMIT 次起判无效
    if challenge == "low" and ctx.low_streak + 1 >= LOW_STREAK_LIMIT:
        return PracticeVerdict(False, "LOW_DIFFICULTY_STREAK", 0,
                               {**base, "challenge": challenge}, challenge)

    # 6 同一任务连续重复：窗口内最近 2 次都是它，本次即第 3 次
    # 「未命名任务」表示用户没命名——不能据此判定是同一任务，不进入重复门。
    named = str(record.task_key or "").strip() not in _UNNAMED_TASK_KEYS
    if named and len(window) >= 2 and window[-1] == record.task_key and window[-2] == record.task_key:
        return PracticeVerdict(False, "REPEATED_TASK", 0,
                               {**base, "challenge": challenge}, challenge)

    # 7 计分
    repeat = named and len(window) >= 1 and window[-1] == record.task_key
    q_bps = record.quality * 100                      # quality/100 -> bps
    f_bps = FEEDBACK_FACTOR_BPS[record.feedback]
    r_bps = REPEAT_FACTOR_BPS if repeat else 10_000

    # 诊断修正：难度改钟形、同日递减、间隔加成。
    # 关闭时两个新因子恒为 10000，分母统一为 10^6，结果与旧口径逐位一致。
    delta = record.difficulty - level_band(ctx.level)
    if shape_enabled:
        m_bps = int(SHAPE_RULES["difficulty_bps"][
            max(-2, min(3, delta))])
        n = max(1, int(ctx.same_day_count))
        same_day_bps = (10_000 * 10_000 //
                        (10_000 + int(SHAPE_RULES["same_day_step_bps"]) * (n - 1)))
        gap_days = idle_seconds // DAY_SECONDS if idle_seconds > 0 else 0
        spacing_bps = int(SHAPE_RULES["spacing_bps"].get(gap_days, 10_000))
    else:
        m_bps = CHALLENGE_FACTOR_BPS[challenge]
        same_day_bps = spacing_bps = 10_000
    raw = (Decimal(20 * record.difficulty)
           * Decimal(q_bps) * Decimal(f_bps) * Decimal(m_bps) * Decimal(r_bps)
           * Decimal(same_day_bps) * Decimal(spacing_bps)
           / Decimal(10_000) ** 6)
    gain = max(1, int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    rec = recovery_bps(record.quality, challenge, idle_days_bps)

    factors = {
        **base,
        "challenge": challenge,
        "level_band": level_band(ctx.level),
        "base_gain": 20 * record.difficulty,
        "quality_bps": q_bps,
        "feedback_bps": f_bps,
        "challenge_bps": m_bps,
        "repeat_bps": r_bps,
        "repeat": repeat,
        "shape_enabled": bool(shape_enabled),
        "difficulty_delta": delta,
        "difficulty_bps": m_bps,
        "same_day_bps": same_day_bps,
        "spacing_bps": spacing_bps,
        "gain_before_skill_factor": gain,
    }
    return PracticeVerdict(True, "OK", gain, factors, challenge,
                           idle_days_bps, base["resumed"], rec)


def assess_level(level: int, stats: dict) -> dict:
    """按可观测达标条件逐项判定某等级是否「能力达标」。

    stats 字段: valid_sessions, quality_sum, feedback_max, max_difficulty, idle_days_bps
    返回逐项明细与 qualified（全部通过）。纯函数。
    """
    criteria = next((c for c in LEVEL_CRITERIA if c["level"] == level), None)
    if criteria is None:
        raise ValueError(f"没有等级 {level} 的达标条件定义")
    sessions = int(stats.get("valid_sessions", 0))
    q_sum = int(stats.get("quality_sum", 0))
    # 诊断修正：均值类门槛改用近期窗口截尾均值。终身均值会把早期的低分
    # 永久钉在当下，导致「每次正好达标」的人数学上永远无法达标。
    wsum = (window_summary(stats.get("recent_sessions"))
            if SHAPE_RULES["enabled"] else None)
    use_window = wsum is not None and wsum["count"] >= 3
    if use_window:
        avg_q = int(wsum["avg_quality"])
        q_label = (f"近 {SHAPE_RULES['window']} 次去最差 "
                   f"{SHAPE_RULES['trim']} 次的平均完成质量")
    else:
        avg_q = q_sum // sessions if sessions > 0 else 0
        q_label = "平均完成质量（历史累计）"
    # 反馈强度与最高难度是「曾经达到过」的成就，保留历史最大值，
    # 不因近期窗口滚动而丢失；避免引入新的卡级死锁。
    feedback_max = int(stats.get("feedback_max", 0))
    max_difficulty = int(stats.get("max_difficulty", 0))
    idle_days_bps = int(stats.get("idle_days_bps", 0))
    max_idle = criteria["max_idle_days"]
    idle_ok = max_idle is None or idle_days_bps <= max_idle * 10_000

    need_sessions = max(0, criteria["min_sessions"] - sessions)
    need_q = max(0, criteria["min_avg_quality"] - avg_q)
    need_d = max(0, criteria["min_difficulty"] - max_difficulty)
    checks = [
        {"key": "min_sessions", "label": "累计有效练习次数",
         "required": criteria["min_sessions"], "actual": sessions,
         "passed": sessions >= criteria["min_sessions"],
         "action": f"再完成 {need_sessions} 次有效练习"},
        {"key": "min_avg_quality", "label": q_label,
         "required": criteria["min_avg_quality"], "actual": avg_q,
         "passed": avg_q >= criteria["min_avg_quality"],
         "action": (f"把{ '近期' if use_window else '历史'}平均完成质量从 "
                    f"{avg_q} 提到 {criteria['min_avg_quality']}"
                    f"（下次质量至少填 {criteria['min_avg_quality']}）"
                    if need_q else "")},
        {"key": "min_feedback", "label": "至少一次客观度量（计时/成绩/仪器/批改）",
         "required": criteria["min_feedback"], "actual": feedback_max,
         "passed": feedback_max >= criteria["min_feedback"],
         "action": ("补一次「客观度量」的练习：用计时、成绩单、仪器读数或批改结果"
                    "作为反馈，而不是自己感觉或他人随口点评"
                    if feedback_max < criteria["min_feedback"] else "")},
        {"key": "min_difficulty", "label": "完成过的最高任务难度(1-5)",
         "required": criteria["min_difficulty"], "actual": max_difficulty,
         "passed": max_difficulty >= criteria["min_difficulty"],
         "action": (f"做一次难度 ≥ {criteria['min_difficulty']} 的任务"
                    if need_d else "")},
        {"key": "max_idle_days", "label": "允许的最长搁置天数",
         "required": "不限" if max_idle is None else max_idle,
         "actual": round(idle_days_bps / 10_000, 2), "passed": idle_ok,
         "action": (f"今天之内练一次，把搁置压回 {max_idle} 天以内"
                    if not idle_ok and max_idle is not None else "")},
    ]
    return {
        "level": level,
        "name": criteria["name"],
        "observable": criteria["observable"],
        "checks": checks,
        "qualified": all(c["passed"] for c in checks),
        "window_used": use_window,
        "window_count": int(wsum["count"]) if wsum else 0,
        "lifetime_avg_quality": q_sum // sessions if sessions > 0 else 0,
    }


# --------------------------------------------------------------------------
# 技能专属规则：只有 4 个正式技能
# --------------------------------------------------------------------------
#
# 重要声明（避免把自定数值当成外部权威结论）：
# 下面所有 min_valid / good / range_* / level_requirements 都是【可配置的
# 默认参数】，用于让判定可运行、可复现、可逐条测试。它们不是运动科学、
# 语言测评或医学标准。接入真实项目时应把这张表换成你自己的课程标准、
# 教练评分表或考试量表；判定逻辑不依赖任何具体数值的「正确性」，
# 只依赖「输入显式、范围显式、比较确定」。
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricSpec:
    """一个可观测指标。全部按「越高越好」建模，整数，避免浮点与方向歧义。

    basis 标明这个指标的数值依据：
      hard   = 数值直接取自官方标准（如 ACP 150 小时、Borg 6–20）
      draft  = 文章示例草案或本系统自定义标尺，需拍板校准
      system = 纯系统标尺（如练习次数、正确率），不宣称外部依据
    """

    key: str
    label: str
    unit: str
    min_valid: int                      # 有效练习最低门槛（含）
    good: int                           # 达到此值，本次加成 +500 bps
    range_low: int                      # 合法值下界（含）
    range_high: int                     # 合法值上界（含）
    level_requirements: tuple[int, ...]  # Lv.1..Lv.8 各自要求的历史平均值下限
    basis: str = "draft"
    source_ids: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if len(self.level_requirements) != MAX_LEVEL:
            raise ValueError(f"{self.key}: 必须给 {MAX_LEVEL} 个等级各一个要求")
        if not (self.range_low <= self.min_valid <= self.range_high):
            raise ValueError(f"{self.key}: min_valid 必须落在合法范围内")
        if not (self.range_low <= self.good <= self.range_high):
            raise ValueError(f"{self.key}: good 必须落在合法范围内")
        if any(b < a for a, b in zip(self.level_requirements,
                                     self.level_requirements[1:])):
            raise ValueError(f"{self.key}: 等级要求必须单调不减")
        if self.basis not in ("hard", "draft", "system"):
            raise ValueError(f"{self.key}: basis 必须是 hard/draft/system")

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "unit": self.unit,
            "min_valid": self.min_valid, "good": self.good,
            "range_low": self.range_low, "range_high": self.range_high,
            "level_requirements": list(self.level_requirements),
            "basis": self.basis, "source_ids": list(self.source_ids), "note": self.note,
        }


# 医学安全门字段：任何启用医学门的技能都必须提交，且必须满足安全条件。
# 依据 Borg RPE 6–20、ACSM 运动前筛查与停止运动警示信号（见 STANDARD_SOURCES）。
MEDICAL_KEYS: tuple[str, ...] = ("pain_flag", "rpe", "stop_sign")


@dataclass(frozen=True)
class SkillRule:
    """一个技能的完整评判标准：每次练习的判定门 + LV1–LV8 达标条件。

    official_max_level：官方硬标准能覆盖到的最高等级。超出部分一律标注
    「无官方对应，待定」，不伪造标准（文章明确要求）。
    """

    skill_id: str
    description: str
    metrics: tuple[MetricSpec, ...]
    level_observables: tuple[str, ...]   # Lv.1..Lv.8 各一条可观测描述
    standard: str = ""
    source_ids: tuple[str, ...] = ()
    evidence_type: str = "draft"          # hard / draft / mixed
    official_max_level: int = MAX_LEVEL
    medical_gate: bool = False
    pain_flag_required: bool = False      # 兼容旧字段；医学门启用时恒为 True
    self_defined_levels: tuple[int, ...] = ()  # 官方空白区中已按拍板自建标准的等级

    def __post_init__(self) -> None:
        if len(self.level_observables) != MAX_LEVEL:
            raise ValueError(f"{self.skill_id}: 必须给 {MAX_LEVEL} 个等级各一条可观测描述")
        if not 1 <= self.official_max_level <= MAX_LEVEL:
            raise ValueError(f"{self.skill_id}: official_max_level 越界")
        keys = [m.key for m in self.metrics]
        if len(set(keys)) != len(keys) or any(k in MEDICAL_KEYS for k in keys):
            raise ValueError(f"{self.skill_id}: 指标 key 重复或占用了医学门字段")
        if not set(self.self_defined_levels) <= set(self.unmapped_levels):
            raise ValueError(f"{self.skill_id}: 自建等级必须落在官方未覆盖区")
        if self.self_defined_levels and any(
            "自建标准" not in self.level_observables[lv - 1]
            for lv in self.self_defined_levels
        ):
            raise ValueError(f"{self.skill_id}: 自建等级描述必须显式标注「自建标准」")
        if self.medical_gate:
            object.__setattr__(self, "pain_flag_required", True)

    @property
    def required_keys(self) -> tuple[str, ...]:
        base = tuple(m.key for m in self.metrics)
        return base + MEDICAL_KEYS if self.medical_gate else base

    @property
    def unmapped_levels(self) -> tuple[int, ...]:
        """官方标准未覆盖的等级（其中一部分可能已按拍板自建标准）。"""
        return tuple(range(self.official_max_level + 1, MAX_LEVEL + 1))

    def mapping_kind(self, level: int) -> str:
        """official=官方硬标准 / self_defined=自建标准·非官方 / unmapped=留空待定。"""
        if level <= self.official_max_level:
            return "official"
        return "self_defined" if level in self.self_defined_levels else "unmapped"

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "description": self.description,
            "standard": self.standard,
            "source_ids": list(self.source_ids),
            "evidence_type": self.evidence_type,
            "official_max_level": self.official_max_level,
            "unmapped_levels": list(self.unmapped_levels),
            "self_defined_levels": list(self.self_defined_levels),
            "mapping_kind_by_level": {
                str(lv): self.mapping_kind(lv) for lv in range(1, MAX_LEVEL + 1)
            },
            "medical_gate": self.medical_gate,
            "metrics": [m.to_dict() for m in self.metrics],
            "level_observables": list(self.level_observables),
            "pain_flag_required": self.pain_flag_required,
            "required_keys": list(self.required_keys),
        }


NO_OFFICIAL_MAPPING = "无官方对应，待定（不伪造阈值，见文章模板要求）"
SELF_DEFINED_MAPPING = "自建标准·非官方（按拍板选项建立，不属于任何官方分级）"

SKILL_RULES: dict[str, SkillRule] = {
    "英语口语": SkillRule(
        skill_id="英语口语",
        description=("等级锚点直接采用 CEFR A1–C2 官方 can-do 描述；每次练习的指标是系统标尺，"
                     "不冒充语言测评标准。LV7–LV8 按拍板结果自建「专业领域 / 近母语」两级，"
                     "达标条件为本系统所定，非 CEFR 或任何官方机构的分级。"),
        standard="CEFR A1–C2（官方 LV1–LV6）+ 自建标准·非官方（LV7–LV8）",
        source_ids=("cefr", "cefr_pdf", "workbuddy_template"),
        evidence_type="hard", official_max_level=6, self_defined_levels=(7, 8),
        metrics=(
            MetricSpec("continuous_minutes", "连续表达时长", "分钟",
                       min_valid=1, good=10, range_low=0, range_high=180,
                       level_requirements=(0, 3, 8, 15, 25, 40, 55, 70),
                       basis="system", note="系统标尺，用于记录单次练习量，非 CEFR 规定值"),
            MetricSpec("active_phrases", "主动使用新表达", "个",
                       min_valid=1, good=8, range_low=0, range_high=100,
                       level_requirements=(0, 2, 5, 10, 18, 30, 45, 60),
                       basis="system"),
            MetricSpec("listener_score", "听者可理解度", "0-100",
                       min_valid=60, good=85, range_low=0, range_high=100,
                       level_requirements=(0, 60, 70, 80, 88, 95, 97, 98),
                       basis="system"),
        ),
        level_observables=(
            "A1：对方放慢并愿意重复时能简单问答，用简单短语描述住处与熟人。",
            "A2：就熟悉任务做简单直接的信息交换，用一串短句描述背景与近况。",
            "B1：应对旅行中多数情境，就熟悉话题加入对话，连贯叙述经历并简述理由。",
            "B2：与母语者自然互动，在熟悉情境参与讨论并论证观点，做清晰详细描述。",
            "C1：流畅自发、几乎不显寻词，灵活用于社交与专业场合，结构化表述复杂主题。",
            "C2：毫不费力参与任何讨论，熟练使用习语，精确传达细微语义并自如改述。",
            "自建标准·非官方｜专业领域：能在专业会议或谈判中连续表达 55 分钟以上，主动调用 45 个以上领域表达，听者可理解度均值 ≥97；能就陌生议题即时组织论证并处理追问。",
            "自建标准·非官方｜近母语：能连续表达 70 分钟以上并自如切换语域，主动使用 60 个以上新表达，可理解度均值 ≥98；能精确传达细微语义、改述与化解误解，结果可被第三方复核。",
        ),
    ),
    "英语读写": SkillRule(
        skill_id="英语读写",
        description=("等级锚点采用 CEFR 阅读/写作官方 can-do；文章另提到中国 CSE 1–9 级，"
                     "本次未取得一手对接表，故不纳入硬标准。LV7–LV8 按拍板结果自建两级，"
                     "达标条件为本系统所定，非官方分级。"),
        standard="CEFR A1–C2（官方 LV1–LV6）+ 自建标准·非官方（LV7–LV8）；CSE 对接待核定",
        source_ids=("cefr", "cefr_pdf", "workbuddy_template"),
        evidence_type="hard", official_max_level=6, self_defined_levels=(7, 8),
        metrics=(
            MetricSpec("words_read", "阅读量", "词",
                       min_valid=50, good=800, range_low=0, range_high=20_000,
                       level_requirements=(0, 200, 600, 1500, 3000, 6000, 9000, 12000),
                       basis="system"),
            MetricSpec("comprehension_score", "理解正确率", "0-100",
                       min_valid=60, good=85, range_low=0, range_high=100,
                       level_requirements=(0, 60, 70, 80, 88, 95, 97, 98),
                       basis="system", note="须来自可核对的题目或批改结果"),
            MetricSpec("words_written", "写作量", "词",
                       min_valid=30, good=400, range_low=0, range_high=10_000,
                       level_requirements=(0, 100, 250, 500, 1000, 2000, 3500, 5000),
                       basis="system"),
        ),
        level_observables=(
            "A1：读懂熟悉名称与极简句；能写明信片、填个人资料表。",
            "A2：读极短简单文本并从日常材料找可预测信息；写简短便条与留言。",
            "B1：读懂以高频日常/职场语言为主的文本；写熟悉主题的连贯短文与个人信件。",
            "B2：读观点性报道与当代文学散文；写清晰详细的说明、议论文并给出利弊。",
            "C1：读长且复杂的实文与文学文本、辨识文体差异；写结构严谨、贴合语境的详细文章。",
            "C2：几乎无障碍阅读各类书面语（含抽象、结构复杂的手册与专业文章），能连贯综述并精确改写。",
            "自建标准·非官方｜专业读写：单次阅读均值 ≥9000 词、理解正确率均值 ≥97、写作均值 ≥3500 词；能综述专业文献并产出可被同行核对的长文。",
            "自建标准·非官方｜近母语读写：单次阅读均值 ≥12000 词、正确率均值 ≥98、写作均值 ≥5000 词；能处理隐含立场与文体差异，产出结构严谨、可复核的长篇文本。",
        ),
    ),
    "西班牙语": SkillRule(
        skill_id="西班牙语",
        description=("等级锚点采用 DELE / CEFR A1–C2（塞万提斯学院，证书终身有效）；"
                     "LV7–LV8 按拍板结果自建两级，达标条件为本系统所定，非 DELE 或 CEFR 官方分级。"),
        standard="DELE A1–C2 / CEFR（官方 LV1–LV6）+ 自建标准·非官方（LV7–LV8）",
        source_ids=("dele", "spanish_dele_map", "cefr", "workbuddy_template"),
        evidence_type="hard", official_max_level=6, self_defined_levels=(7, 8),
        metrics=(
            MetricSpec("oral_minutes", "西语连续表达时长", "分钟",
                       min_valid=1, good=10, range_low=0, range_high=180,
                       level_requirements=(0, 3, 8, 15, 25, 40, 55, 70),
                       basis="system"),
            MetricSpec("dele_mock_score", "DELE 模考正确率", "0-100",
                       min_valid=60, good=85, range_low=0, range_high=100,
                       level_requirements=(0, 60, 70, 80, 88, 95, 97, 98),
                       basis="system", note="模考分数不等于正式 DELE 证书"),
            MetricSpec("words_read", "西语阅读量", "词",
                       min_valid=30, good=600, range_low=0, range_high=20_000,
                       level_requirements=(0, 100, 300, 800, 1500, 3000, 5000, 8000),
                       basis="system"),
        ),
        level_observables=(
            "DELE A1：简单问候、自我介绍与旅游基础短句，理解慢速清晰的基本表达。",
            "DELE A2：日常购物出行与简单对话，理解与个人/家庭/购物/职业相关的高频表达。",
            "DELE B1：独立出行、讨论熟悉话题，理解工作学习休闲场合清晰标准语的主要内容。",
            "DELE B2：应付无特殊语言要求的普通交流，理解复杂文章要点并流利自然交流。",
            "DELE C1：无阻碍清晰表达，广泛使用文学与口语词汇，胜任正式工作沟通。",
            "DELE C2：应付高语言要求场景，熟知语言背后的文化，精确区分细微语义。",
            "自建标准·非官方｜专业西语：连续表达均值 ≥55 分钟、DELE 模考正确率均值 ≥97、阅读均值 ≥5000 词；能在专业场合即时沟通并复核书面产出。",
            "自建标准·非官方｜近母语西语：连续表达均值 ≥70 分钟、模考正确率均值 ≥98、阅读均值 ≥8000 词；能处理文化隐含与语域差异，产出可被母语者核对的文本。",
        ),
    ),
    "电脑办公软件技术": SkillRule(
        skill_id="电脑办公软件技术",
        description="等级锚点按 ICDL 基础模块 → MOS Associate → MOS Expert 公开阶梯；LV6–LV8 无官方对应。",
        standard="ICDL / Microsoft Office Specialist Associate → Expert",
        source_ids=("mos", "mos_pathway", "workbuddy_template"),
        evidence_type="hard", official_max_level=5,
        metrics=(
            MetricSpec("tasks_completed", "完成任务数", "个",
                       min_valid=1, good=8, range_low=0, range_high=200,
                       level_requirements=(0, 3, 8, 16, 30, 50, 80, 120),
                       basis="system"),
            MetricSpec("accuracy_score", "操作正确率", "0-100",
                       min_valid=60, good=90, range_low=0, range_high=100,
                       level_requirements=(0, 60, 70, 80, 88, 95, 97, 98),
                       basis="system"),
            MetricSpec("cert_level", "已取得认证等级", "0-3",
                       min_valid=0, good=2, range_low=0, range_high=3,
                       level_requirements=(0, 1, 2, 2, 3, 3, 3, 3),
                       basis="draft", source_ids=("mos_pathway",),
                       note="0=无 / 1=ICDL / 2=MOS Associate / 3=MOS Expert；映射为本系统约定，非微软官方等级编号"),
        ),
        level_observables=(
            "ICDL 基础：理解文字处理、电子表格、在线协作与 IT 安全概念，在指导下完成简单任务。",
            "MOS Associate：任一应用达到 Associate 考纲，独立创建管理文档、公式函数与图表。",
            "MOS Associate 完整：3 门不同应用（至少 1 门 M365）通过，能处理常规办公任务组合。",
            "MOS Expert：先持 Associate，再取得 2 门不同应用 Expert，处理长文档、高级数据分析与自动化。",
            "MOS Expert / Master 级：跨应用复杂方案，能设计模板与协作流程并复核他人产出。",
            NO_OFFICIAL_MAPPING, NO_OFFICIAL_MAPPING, NO_OFFICIAL_MAPPING,
        ),
    ),
    "剪辑技术": SkillRule(
        skill_id="剪辑技术",
        description=("LV1–LV2 依据 Adobe ACP Premiere Pro 官方考试指南（入门级、约 150 小时、"
                     "50 分钟应用内考试）。LV3–LV8 按拍板结果改用「可核对交付物」口径："
                     "交付作品数量 + 客户验收通过率，取代原文章的时长/素材数草案。"
                     "这套阈值是本系统自建标准，不是 Adobe 或任何行业机构的官方分级。"),
        standard="Adobe ACP Premiere Pro（LV1–LV2）+ 可核对交付物自建标准（LV3–LV8，非官方）",
        source_ids=("adobe_acp", "adobe_acp_cn", "workbuddy_template"),
        evidence_type="mixed", official_max_level=2,
        self_defined_levels=(3, 4, 5, 6, 7, 8),
        metrics=(
            MetricSpec("deliverables", "本次交付作品数", "件",
                       min_valid=1, good=3, range_low=0, range_high=50,
                       level_requirements=(0, 1, 2, 4, 7, 11, 16, 22),
                       basis="system",
                       note="拍板改为可核对交付物；件数须对应实际交付记录，不按成片时长折算"),
            MetricSpec("client_acceptance", "客户验收通过率", "0-100",
                       min_valid=60, good=90, range_low=0, range_high=100,
                       level_requirements=(0, 60, 70, 80, 88, 95, 97, 98),
                       basis="system",
                       note="交付作品中被需求方验收通过的比例；须有验收记录，不接受自评代替"),
            MetricSpec("delivery_score", "交付质量评分", "0-100",
                       min_valid=60, good=90, range_low=0, range_high=100,
                       level_requirements=(0, 60, 70, 80, 88, 95, 97, 98),
                       basis="system",
                       note="按自报处理；数据来源已定稿为自报，系统不接收第三方评分"),
        ),
        level_observables=(
            "ACP 准入：了解视频行业协作、项目设置与界面，能套模板完成 1 分钟内短片。",
            "ACP 认证：约 150 小时教学+实操、50 分钟应用内考试；能组织项目、创建修改视觉元素并按平台要求导出。",
            "自建标准·非官方：累计交付 ≥3 件可核对作品，客户验收通过率均值 ≥70，能按反馈完成返工并留记录。",
            "自建标准·非官方：累计交付 ≥8 件，验收通过率均值 ≥80，能独立负责中长内容并对交付时间负责。",
            "自建标准·非官方：累计交付 ≥16 件、验收通过率均值 ≥88，能同时管理多条内容线并复核他人产出。",
            "自建标准·非官方：累计交付 ≥28 件、验收通过率均值 ≥95，能定义交付规范与验收清单并稳定复现结果。",
            "自建标准·非官方：跨项目建立可复用的交付与验收流程，结果能被第三方按记录复核。",
            "自建标准·非官方：能为他人设计交付标准与培训方案，多个项目结果一致且可度量复现。",
        ),
    ),
    "跑步技术": SkillRule(
        skill_id="跑步技术",
        description=("安全门依据 Borg RPE 6–20、ACSM 运动前筛查与停止信号；步频依据 ACSM"
                     "『提高 5–10% 后通常 >170 spm』。按拍板结果不设配速硬门槛，"
                     "也不采纳『全马破 4』为达标条件；距离阈值仅为练习量标尺，非官方能力等级。"),
        standard="ACSM 跑步技术原则 + Borg RPE + 距离练习量标尺（非官方能力等级）",
        source_ids=("acsm_running", "borg_rpe", "acsm_screening", "stop_signs",
                    "workbuddy_template"),
        evidence_type="draft", official_max_level=8, medical_gate=True,
        metrics=(
            MetricSpec("distance_meters", "单次距离", "米",
                       min_valid=500, good=5_000, range_low=0, range_high=100_000,
                       level_requirements=(0, 2_000, 5_000, 10_000, 20_000, 30_000, 42_195, 50_000),
                       basis="draft", note="文章草案；ACSM 未规定能力等级距离"),
            MetricSpec("cadence_spm", "平均步频", "步/分",
                       min_valid=140, good=170, range_low=0, range_high=250,
                       level_requirements=(0, 150, 160, 165, 170, 170, 175, 175),
                       basis="hard", source_ids=("acsm_running",),
                       note="ACSM：较习惯步频提高 5–10% 通常使步频 >170；并明确不存在通用 gold standard"),
            MetricSpec("form_score", "技术动作评分", "0-100",
                       min_valid=60, good=85, range_low=0, range_high=100,
                       level_requirements=(0, 60, 70, 80, 88, 95, 97, 98),
                       basis="system", note="按自评处理；系统不采集视频，如需高可信度可后续接视频复核"),
        ),
        level_observables=(
            "在直接监督下完成走跑结合，理解 RPE 与停止运动信号。",
            "能连续跑 2–5 km，步频较习惯值提高 5–10%（ACSM 常见结果 >170 spm）。",
            "能独立执行周跑量增幅 ≤10% 的计划，保持落地位置靠近重心。",
            "能完成 10 km 并按 RPE 11–14 控制强度，借助视频/教练反馈修正动作。",
            "能完成半马/长距离，力量训练覆盖主要肌群每周 ≥2 次（ACSM 2026）。",
            "能完成全马并讲清技术要点；按拍板结果不设配速硬门槛，仍以距离、ACSM 步频与 Borg RPE 安全门为判定依据。",
            "能按 RPE 与恢复状态编排长距离训练周期，独立修正落地与步频，并用可核对记录说明调整依据。",
            "能为他人设计并复盘跑步技术方案，多周期结果稳定可度量，且不以提高成绩为唯一晋级条件。",
        ),
    ),
    "健身技术": SkillRule(
        skill_id="健身技术",
        description=("依据 ACSM 2009 进阶模型（新手 8–12RM、2–3 次/周、2–10% 加负荷）"
                     "与 2026 Position Stand（主要肌群每周 ≥2 次、力量 80% 1RM×2–3 组、"
                     "增肌约 10 组/肌群/周）。按拍板结果不采用文章的相对力量倍数草案；"
                     "LV6–LV8 改为可观测的训练设计与安全执行能力。"),
        standard="ACSM Position Stand 2009 / 2026（不采用相对力量倍数草案）",
        source_ids=("acsm_rt2009", "acsm_rt2026", "borg_rpe", "acsm_screening",
                    "stop_signs", "workbuddy_template"),
        evidence_type="mixed", official_max_level=8, medical_gate=True,
        metrics=(
            MetricSpec("working_sets", "有效组数", "组",
                       min_valid=1, good=10, range_low=0, range_high=60,
                       level_requirements=(0, 2, 6, 10, 16, 24, 32, 40),
                       basis="hard", source_ids=("acsm_rt2026",),
                       note="ACSM 2026：力量 2–3 组/动作；增肌约 10 组/肌群/周；首要结论是坚持"),
            MetricSpec("reps_per_set", "每组次数", "次",
                       min_valid=5, good=12, range_low=0, range_high=50,
                       level_requirements=(0, 8, 8, 10, 12, 12, 12, 12),
                       basis="hard", source_ids=("acsm_rt2009",),
                       note="ACSM 2009：新手 8–12RM；能多做 1–2 次时按 2–10% 加负荷"),
            MetricSpec("form_score", "动作质量评分", "0-100",
                       min_valid=60, good=85, range_low=0, range_high=100,
                       level_requirements=(0, 60, 70, 80, 88, 95, 97, 98),
                       basis="system", note="按自评处理；与「数据来源=自报」一致，系统不采集视频"),
        ),
        level_observables=(
            "新手：在指导下以 8–12RM 负荷完成基础动作，每周 2–3 次（ACSM 2009）。",
            "能独立完成主要肌群每周 ≥2 次的规律训练（ACSM 2026：坚持比复杂方案更重要）。",
            "约 6 个月规律训练，进入 1–12RM 周期化；能多做 1–2 次时按 2–10% 加负荷。",
            "力量 80% 1RM×2–3 组；增肌约 10 组/肌群/周（ACSM 2026），强调个体化。",
            "能按目标编排顺序、容量与频率，用视频复核动作标准并调整方案。",
            "能按 RM 与每周频率设计周期化方案，记录负荷递进（多做 1–2 次时按 2–10% 加负荷），并按 Borg RPE 与停止信号调整；无体重倍数门槛。",
            "能为不同训练目标设计并复盘完整方案，指导他人安全执行，且训练记录可被第三人核对（ACSM 原则的可观测应用）。",
            "能建立可复用的训练与评估流程，跨多人稳定复现结果，并说明各阶段负荷与频率的依据。",
        ),
    ),
}

SKILL_IDS: tuple[str, ...] = tuple(SKILL_RULES)
GOOD_BONUS_BPS = 500  # 每有一个指标达到 good 线，技能系数 +500 bps


# --------------------------------------------------------------------------
# 拍板记录：清单由代码自动枚举，人工只负责把「问题与选项」说成人话。
# 12 项已于 2026-11-06 全部拍板并实施，见 APPLIED_DECISIONS / IMPLEMENTATION_NOTES。
# 每项 evidence 必须指向真实存在的条目；测试会强制校验「不漏列、不编造」。
# --------------------------------------------------------------------------

_SYSTEM_PARAM_NAMES: tuple[str, ...] = (
    "MIN_QUALITY", "NO_FEEDBACK_QUALITY_GATE", "LOW_STREAK_LIMIT",
    "REPEAT_WINDOW", "RESUME_IDLE_DAYS", "DECAY_GRACE_SECONDS",
    "RETENTION_FLOOR_BPS", "GOOD_BONUS_BPS", "FEEDBACK_FACTOR_BPS",
    "CHALLENGE_FACTOR_BPS", "REPEAT_FACTOR_BPS", "DECAY_ANCHORS",
    "RPE_MIN", "RPE_MAX", "RPE_VIGOROUS", "RPE_REJECT_ABOVE",
)


def collect_open_items() -> dict:
    """扫描 SKILL_RULES 与本模块常量，列出真实存在的「未定项」。

    四类：basis != hard 的指标、官方未覆盖等级、含「待拍板」的等级描述、
    本系统自定参数。新增任何一类都会自动出现在这里，不靠手工维护清单。
    """
    metrics, levels, pending = [], [], []
    for sid, rule in SKILL_RULES.items():
        for m in rule.metrics:
            if m.basis != "hard":
                metrics.append({
                    "ref": f"metric:{sid}.{m.key}", "skill_id": sid,
                    "label": m.label, "basis": m.basis,
                    "min_valid": m.min_valid, "good": m.good,
                    "range": [m.range_low, m.range_high], "note": m.note,
                })
        for lv in rule.unmapped_levels:
            levels.append({
                "ref": f"level:{sid}.LV{lv}", "skill_id": sid, "level": lv,
                "text": rule.level_observables[lv - 1],
            })
        for i, text in enumerate(rule.level_observables, 1):
            if "待拍板" in text:
                pending.append({
                    "ref": f"pending:{sid}.LV{i}", "skill_id": sid,
                    "level": i, "text": text,
                })
    parameters = [
        {"ref": f"param:{name}", "value": globals()[name]}
        for name in _SYSTEM_PARAM_NAMES
    ]
    return {
        "metrics": metrics, "unmapped_levels": levels,
        "pending_descriptions": pending, "system_parameters": parameters,
    }


def expand_evidence(tokens: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """把 evidence 通配（如 metric:英语口语*）展开成具体引用，非法引用直接报错。"""
    all_refs = {x["ref"] for group in collect_open_items().values() for x in group}
    out: set[str] = set()
    for token in tokens:
        if token in all_refs:
            out.add(token)
        elif token.endswith("*"):
            prefix = token[:-1]
            found = [r for r in all_refs if r.startswith(prefix)]
            if not found:
                raise ValueError(f"evidence 无匹配条目: {token}")
            out.update(found)
        else:
            raise ValueError(f"evidence 引用了不存在的条目: {token}")
    return tuple(sorted(out))


# 已定稿的 11 项（DECISION_POINTS）不在此重复。下面是代码里真实存在、
# 但没有任何官方标准可依、必须由使用者拍板的开放项。
OPEN_DECISIONS: tuple[dict, ...] = (
    {
        "id": "language_metric_scale", "group": "语言", "impact": "高",
        "question": "英语口语 / 英语读写 / 西班牙语 的 9 个练习量指标，数值怎么定？",
        "current": "系统自定：口语连续 1 分钟起算 / 10 分钟优秀；阅读 50 词起算 / 800 词优秀；写作 30 词起算 / 400 词优秀等。CEFR 只给能力描述，不给「每次练几分钟、多少词」。",
        "evidence": ["metric:英语口语*", "metric:英语读写*", "metric:西班牙语*"],
        "code_refs": ["realistic_rules.SKILL_RULES[英语口语/英语读写/西班牙语].metrics"],
        "options": [
            {"key": "keep", "label": "保留这套系统标尺（推荐）", "effect": "判定可运行、可复现；数值明确标注为系统自定，不冒充语言测评标准"},
            {"key": "can_do", "label": "改成 CEFR can-do 自评等级", "effect": "口径更官方；但自评等级是主观分，每日练习量会失去可核对的数字"},
            {"key": "custom", "label": "换成使用者自己的课时/作业标准", "effect": "最贴合真实课程；需要你提供每项的门槛与优秀线数值"},
        ],
        "recommended": "keep",
        "rationale": "官方标准管「能做到什么」，管不了「今天练了几分钟」。硬把 CEFR 套成分钟数就是编数据。留着标尺、标清来源最诚实。",
        "affects": "每次练习是否计入、优秀线加成、各级达标校验",
    },
    {
        "id": "office_cert_mapping", "group": "办公软件", "impact": "中",
        "question": "办公软件的证书等级映射，以及官方空白的 LV6–LV8 怎么办？",
        "current": "cert_level：0=无 / 1=ICDL / 2=MOS Associate / 3=MOS Expert，是本系统约定编号，非微软官方等级。LV6–LV8 标「无官方对应，待定」。",
        "evidence": ["metric:电脑办公软件技术*", "level:电脑办公软件技术*"],
        "code_refs": ["realistic_rules.SKILL_RULES[电脑办公软件技术]"],
        "options": [
            {"key": "keep", "label": "保留映射并在界面标明「非官方编号」（推荐）", "effect": "证书只作达标证据之一，不冒充微软分级"},
            {"key": "drop", "label": "去掉 cert_level 指标", "effect": "判定更纯：只看完成任务数与正确率；但证书信息不再进入达标校验"},
            {"key": "freeze5", "label": "等级上限冻结在 LV5", "effect": "不出现「待定」等级；但 8 级框架对该技能只剩 5 格"},
        ],
        "recommended": "keep",
        "rationale": "微软只到 Expert，往上没有官方台阶。保留映射能当证据，又不会把自编号说成官方。",
        "affects": "办公技能每次练习的必填字段、LV6–LV8 是否可升",
    },
    {
        "id": "edit_level_thresholds", "group": "剪辑", "impact": "高",
        "question": "剪辑 LV3–LV8 的晋升标准用什么？",
        "current": "现在：交付作品数 1 件起算 / 3 件优秀，客户验收通过率 60 起算 / 90 优秀，交付质量自评；LV3–LV8 按可核对交付物自建标准并标注非官方。Adobe ACP 官方只覆盖入门级。拍板前用的是成片时长 / 素材数的文章草案。",
        "evidence": ["metric:剪辑技术*", "level:剪辑技术*"],
        "code_refs": ["realistic_rules.SKILL_RULES[剪辑技术].level_observables"],
        "options": [
            {"key": "keep_draft", "label": "保留文章草案，全程标注「非官方」（推荐）", "effect": "有可用的晋升描述；但时长、奖项认定都不是权威标准"},
            {"key": "deliverables", "label": "换成可核对交付物：作品数量 + 客户验收", "effect": "比时长更贴近现实；需要新增指标与判定逻辑"},
            {"key": "freeze2", "label": "冻结在 LV2，以上全部留白", "effect": "最严谨；但剪辑技能在 8 级框架里几乎没成长空间"},
        ],
        "recommended": "keep_draft",
        "rationale": "文章自己标了「示例、需拍板」，Adobe 官方只有入门级。保留并显著标注，比删掉或编一套行业等级更实在。",
        "affects": "剪辑 6 个等级描述、3 个指标数值、LV3 以上能否判定达标",
    },
    {
        "id": "run_distance_scale", "group": "跑步", "impact": "高",
        "question": "跑步的距离门槛与「全马破 4」怎么处理？",
        "current": "距离 500 米起算 / 5000 米优秀，各级要求 0/2000/5000/10000/20000/30000/…米，标为文章草案；ACSM 未规定能力等级距离。按拍板结果不采纳「全马破 4」，LV6–LV8 描述已改为不设配速硬门槛。",
        "evidence": ["metric:跑步技术*"],
        "code_refs": ["realistic_rules.SKILL_RULES[跑步技术]"],
        "options": [
            {"key": "keep", "label": "保留距离草案，不采纳「破 4」为硬门槛（推荐）", "effect": "距离只是练习量尺；能力判定仍靠 ACSM 步频 + Borg RPE 安全门"},
            {"key": "adopt_sub4", "label": "把「全马破 4」写成 LV6 证据项", "effect": "有社区共识；但它是跑圈门槛不是运动科学标准，且需要完赛成绩数据"},
            {"key": "no_distance", "label": "去掉距离，只留步频 + 安全门", "effect": "全部阈值有出处；但少了训练量维度，等级区分变粗"},
        ],
        "recommended": "keep",
        "rationale": "ACSM 明确没有通用的能力等级距离表。距离当练习量记没问题，当成水平证书就是瞎编。",
        "affects": "跑步每次练习是否计入、各级距离均值要求、LV6 描述",
    },
    {
        "id": "strength_high_levels", "group": "健身", "impact": "高",
        "question": "健身 LV6–LV8 的相对力量倍数草案是否采用？",
        "current": "按拍板结果不采用「深蹲 1RM÷体重」等相对力量倍数草案；LV6–LV8 已改为 ACSM 可观测的训练设计与安全执行能力。现行指标只有有效组数、每组次数、自评动作分（按 ACSM 次数/频率原则）。",
        "evidence": ["metric:健身技术*"],
        "code_refs": ["realistic_rules.SKILL_RULES[健身技术].level_observables"],
        "options": [
            {"key": "reject", "label": "不采用，只保留 ACSM 次数/频率（推荐）", "effect": "全部有 Position Stand 出处；高阶等级描述需改成可观测的训练设计能力"},
            {"key": "squat_only", "label": "采用，且只用深蹲 1RM÷体重", "effect": "口径单一；但阈值仍是草案，且需新增 1RM 与体重指标"},
            {"key": "three_lifts", "label": "采用三大项总和 + 训练年限", "effect": "更贴近力量圈；新增数据最多，且年限与能力无必然关系"},
        ],
        "recommended": "reject",
        "rationale": "文章自己写了「倍数阈值为示例，非官方」。定成硬指标等于把草案升级成标准。",
        "affects": "健身 LV6–LV8 描述、是否新增体重/1RM 字段",
    },
    {
        "id": "unmapped_language_levels", "group": "语言", "impact": "中",
        "question": "英语与西语 LV7–LV8 的空白，保持什么状态？",
        "current": "现在：三个语言技能 LV7–LV8 自建「专业领域 / 近母语」两级，含具体可观测达标条件，mapping_kind=self_defined 并标注非官方；经验可累积到 8 级。拍板前这两级是留白待定。",
        "evidence": ["level:英语口语*", "level:英语读写*", "level:西班牙语*"],
        "code_refs": ["realistic_rules.NO_OFFICIAL_MAPPING", "proficiency.ProficiencySystem.assess"],
        "options": [
            {"key": "leave_blank", "label": "保持待定（推荐）", "effect": "CEFR 最高 C2，再往上就是自建标准"},
            {"key": "freeze6", "label": "语言技能等级上限冻结在 LV6", "effect": "界面不出现待定等级；与统一 8 级框架不一致"},
            {"key": "self_defined", "label": "自建「专业领域 / 近母语」两级", "effect": "补齐 8 格；必须显著标注非官方，且需要你给达标条件"},
        ],
        "recommended": "leave_blank",
        "rationale": "这一项上一轮已定稿为留空，此处是确认要不要推翻。没有新证据就不翻案。",
        "affects": "语言技能能否升到 LV7–LV8、达标校验是否显示待定标记",
    },
    {
        "id": "growth_curve", "group": "系统数值", "impact": "高",
        "question": "「先快后慢」有多陡？",
        "current": "默认 PowerRule(100, 1.5)：升 2 级要 100 点，之后 283 / 520 / 800 / 1119…网页各技能另设 base 90–120、k 1.35–1.6，互相不同。这些数值无任何官方出处。",
        "evidence": [],
        "code_refs": ["proficiency.SkillConfig", "web_app.DEFAULT_SKILLS"],
        "options": [
            {"key": "keep", "label": "保留现值（推荐）", "effect": "新手几天连升几级，之后明显变慢；已在 83 个单测与示例中验证"},
            {"key": "steeper", "label": "更陡（k 提到 1.8）", "effect": "高阶更难，长期目标感强；容易让人觉得卡住"},
            {"key": "gentler", "label": "更缓（k 降到 1.3）", "effect": "升级更顺；「入门容易精通难」被削弱"},
        ],
        "recommended": "keep",
        "rationale": "k>1 的形状有道理，具体 1.5 没有科学依据，纯粹是可调手感参数。没有实测数据前别乱调。",
        "affects": "全部 7 个技能的升级速度与每次练习的等级收益",
    },
    {
        "id": "decay_speed", "group": "系统数值", "impact": "中",
        "question": "生疏速度与保底，按现在这套走吗？",
        "current": "1 天宽限期内不掉；7 天 95%、30 天 78%、120 天 50%、365 天起最低 35%；搁置 ≥14 天记为中断恢复。锚点为本系统设定，非医学标准。",
        "evidence": ["param:DECAY_ANCHORS", "param:DECAY_GRACE_SECONDS",
                     "param:RETENTION_FLOOR_BPS", "param:RESUME_IDLE_DAYS"],
        "code_refs": ["realistic_rules.DECAY_ANCHORS"],
        "options": [
            {"key": "keep", "label": "保留现值（推荐）", "effect": "半年不练掉一半，捡回来比重新学快，符合常识但非临床结论"},
            {"key": "lenient", "label": "更宽容（1 年最低 50%）", "effect": "少掉级；弱化「长期不练会生疏」"},
            {"key": "strict", "label": "更严格（1 年最低 20%）", "effect": "退化明显；可能让老用户挫败"},
        ],
        "recommended": "keep",
        "rationale": "遗忘曲线没有可直接照搬的固定锚表。这套是手感参数，改之前得有真实使用数据。",
        "affects": "保持度、有效熟练度、退化与恢复速度",
    },
    {
        "id": "gain_weights", "group": "系统数值", "impact": "中",
        "question": "一次练习加多少，权重怎么分？",
        "current": "基数 20×难度；反馈系数 0.5/0.8/1.0/1.25；难度匹配 1.0/0.5/0.15；重复任务 0.2；每项达优秀线 +5%，三项 +15%。全部为系统自定系数。实测难度 3 / 质量 80 / 他人反馈得 48 点，三项全达优秀线得 55 点。",
        "evidence": ["param:GOOD_BONUS_BPS", "param:FEEDBACK_FACTOR_BPS",
                     "param:CHALLENGE_FACTOR_BPS", "param:REPEAT_FACTOR_BPS"],
        "code_refs": ["realistic_rules.evaluate_practice"],
        "options": [
            {"key": "keep", "label": "保留现值（推荐）", "effect": "实测难度3/质量80/他人反馈得 48 点，全达优秀线得 55 点"},
            {"key": "feedback_first", "label": "加大反馈权重", "effect": "鼓励找人点评或用数据核对；自练路径收益下降"},
            {"key": "no_bonus", "label": "取消优秀线加成", "effect": "公式更简单；做得特别好和不凑合收益一样"},
        ],
        "recommended": "keep",
        "rationale": "权重是设计取舍不是科学结论。现值已能区分敷衍、常规、高质量三种练法。",
        "affects": "每次练习的增益点数",
    },
    {
        "id": "validity_gates", "group": "系统数值", "impact": "中",
        "question": "「什么算一次有效练习」的门槛，宽还是紧？",
        "current": "质量 <40 判无效；无反馈时质量 <70 判无效；连续第 3 次低难度判无效；最近 3 次同一任务判无效。均为系统自定。",
        "evidence": ["param:MIN_QUALITY", "param:NO_FEEDBACK_QUALITY_GATE",
                     "param:LOW_STREAK_LIMIT", "param:REPEAT_WINDOW"],
        "code_refs": ["realistic_rules.evaluate_practice"],
        "options": [
            {"key": "keep", "label": "保留现值（推荐）", "effect": "能挡住敷衍、机械重复、刷低难度；偶尔认真但没反馈的记录可能被挡"},
            {"key": "loose", "label": "放宽（质量门槛降到 30、重复窗口放到 5）", "effect": "少误伤；刷量更容易"},
            {"key": "tight", "label": "收紧（无反馈一律不计入）", "effect": "数据更可信；纯自学会寸步难行"},
        ],
        "recommended": "keep",
        "rationale": "没有外部审核时，门槛是唯一防刷手段。现值不算苛刻。",
        "affects": "哪些练习被判无效、经验累计速度",
    },
    {
        "id": "medical_reject_line", "group": "医学安全", "impact": "高",
        "question": "RPE 累到多少就不计入？",
        "current": "Borg RPE 6–20 是官方量表；「≥14 属剧烈」有 ACSM 出处；但「RPE >17 一律拒绝」是本系统设的安全线，不是医学禁忌值。疼痛、胸口疼/头晕等停止信号一律拒绝。",
        "evidence": ["param:RPE_MIN", "param:RPE_MAX", "param:RPE_VIGOROUS",
                     "param:RPE_REJECT_ABOVE"],
        "code_refs": ["realistic_rules.evaluate_skill_metrics"],
        "options": [
            {"key": "keep", "label": "保留 17（推荐）", "effect": "偏保守，安全优先；接近力竭的训练不计入"},
            {"key": "raise19", "label": "放宽到 19", "effect": "高强度训练能计入；需你确认这不是在鼓励带伤硬练"},
            {"key": "warn_only", "label": "只警告不拒绝", "effect": "数据完整；但失去安全门，风险由使用者自担"},
        ],
        "recommended": "keep",
        "rationale": "安全门宁可保守。17 是系统自定的保守线，我不把它说成医学规定。",
        "affects": "跑步/健身高强度训练是否计入",
    },
    {
        "id": "assess_as_gate", "group": "系统架构", "impact": "高",
        "question": "「可观测达标校验」要不要真的卡升级？",
        "current": "按拍板结果已改为硬门槛：练习路径每次升级都逐级校验目标等级的可观测达标条件，未通过则卡在原级（gate_blocked=True，界面列出缺项）。管理注入/回档接口显式绕过。升级不再只看有效熟练度。",
        "evidence": [],
        "code_refs": ["proficiency.ProficiencySystem.assess",
                      "proficiency.ProficiencySystem.add_practice"],
        "options": [
            {"key": "display_only", "label": "维持只展示（推荐）", "effect": "数字升得快，界面同时提醒「这一级其实还没真达标」，两不耽误"},
            {"key": "hard_gate", "label": "未达标不许升级", "effect": "等级最可信；但次数/均质/搁置天数这些门槛会明显拖慢升级"},
            {"key": "hide", "label": "从界面去掉达标校验", "effect": "最简洁；丢掉「等级不等于能力」的提醒"},
        ],
        "recommended": "display_only",
        "rationale": "改成硬门槛会牵动全部升级路径和 83 个测试。当前展示方式已经把差异摆出来了，不必为「更严格」付这个代价。",
        "affects": "升级路径、卡片上的达标提示",
    },
)


# 使用者 2026-11-06 逐项拍板的最终结果。这里是规则规格的一部分：
# 网页存档 decisions.json 必须与本表完全一致，测试会强制比对。
APPLIED_DECISIONS: dict[str, str] = {
    "language_metric_scale": "keep",
    "office_cert_mapping": "keep",
    "edit_level_thresholds": "deliverables",
    "run_distance_scale": "keep",
    "strength_high_levels": "reject",
    "unmapped_language_levels": "self_defined",
    "growth_curve": "keep",
    "decay_speed": "keep",
    "gain_weights": "keep",
    "validity_gates": "keep",
    "medical_reject_line": "keep",
    "assess_as_gate": "hard_gate",
}

IMPLEMENTATION_NOTES: dict[str, str] = {
    "language_metric_scale": "维持 9 个系统标尺指标与 basis=system 标注，不改动。",
    "office_cert_mapping": "维持 cert_level 映射并继续标注非微软官方编号；LV6–LV8 保持待定。",
    "edit_level_thresholds": "已改：成片时长/素材数换成交付作品数与客户验收通过率；LV3–LV8 改为可核对交付物口径，标注自建标准·非官方。",
    "run_distance_scale": "维持距离练习量标尺；已改 LV6–LV8 描述，明确不设配速硬门槛、不采纳全马破 4。",
    "strength_high_levels": "已改：健身 LV6–LV8 移除相对力量倍数草案，改为 ACSM 可观测的训练设计与安全执行能力。",
    "unmapped_language_levels": "已改：三个语言技能 LV7/LV8 自建两级，含具体可观测达标条件；mapping_kind=self_defined 并显著标注非官方。",
    "growth_curve": "维持默认 PowerRule(100, 1.5) 与各技能 base 90–120 / 幂次 1.35–1.6。",
    "decay_speed": "维持生疏锚点、1 天宽限期、3500 bps 下限与 14 天中断恢复判定。",
    "gain_weights": "维持基数 20×难度、反馈/重复系数与每项达优秀线 +5% 加成。2026-11-08 诊断修正（用户新指令，晚于本次拍板）：难度收益改钟形、新增同日递减与 1–7 天间隔加成，默认开启；shape_enabled=False 可完整回退到本条原口径。与 keep 的差异在 DIAGNOSIS_VERIFICATION 与规则页如实标注，不假装一致。",
    "validity_gates": "维持质量 40、无反馈 70、连续低难度 3 次、重复任务窗口 3 次。",
    "medical_reject_line": "维持 RPE>17 拒绝及疼痛/停止信号的零副作用拒绝。",
    "assess_as_gate": "已改：核心引擎新增 assess_gate（默认开启）。练习路径升级前逐级校验目标等级达标条件，未通过则卡级并显示缺项；管理注入/回档路径显式绕过。2026-11-08 修正：均值类门槛改近期窗口截尾均值 + 连续 3 次逐次达标快通，消除「按标准练永远差一点」的数学死锁；终身均值保留为历程页履历。",
}

for _item in OPEN_DECISIONS:
    _item["selected"] = APPLIED_DECISIONS[_item["id"]]
    _item["status"] = "applied"
    _item["implementation"] = IMPLEMENTATION_NOTES[_item["id"]]


def open_decision_report() -> dict:
    """导出拍板结果 + 自动枚举证据，供网页与测试核对。"""
    auto = collect_open_items()
    covered: set[str] = set()
    for item in OPEN_DECISIONS:
        covered.update(expand_evidence(item["evidence"]))
    all_refs = {x["ref"] for group in auto.values() for x in group}
    return {
        "status": "applied",
        "count": len(OPEN_DECISIONS),
        "decisions": [dict(x) for x in OPEN_DECISIONS],
        "selections": dict(APPLIED_DECISIONS),
        "implementation_notes": dict(IMPLEMENTATION_NOTES),
        "evidence_items": auto,
        "evidence_covered": sorted(covered),
        "evidence_uncovered": sorted(all_refs - covered),
        "code_refs": sorted({r for x in OPEN_DECISIONS for r in x["code_refs"]}),
    }


@dataclass(frozen=True)
class MetricVerdict:
    valid: bool
    reason_code: str
    skill_factor_bps: int = 10_000
    checks: tuple[dict, ...] = ()


def skill_rule_for(skill_id: str,
                   extra_rules: dict[str, SkillRule] | None = None
                   ) -> SkillRule | None:
    """按技能名取专属规则；未定义时返回 None（通用兜底，不视为正式技能）。

    extra_rules 由调用方按租户注入「用户自定义技能」的规则表；缺省时行为
    与原来逐位一致（只查内置技能）。注入只影响查找，不修改模块级
    SKILL_RULES，多个用户之间的规则表互不共享。
    """
    if extra_rules and skill_id in extra_rules:
        return extra_rules[skill_id]
    return SKILL_RULES.get(skill_id)


def evaluate_skill_metrics(rule: SkillRule, metrics: object) -> MetricVerdict:
    """每次熟练度增加的【技能专属判断标准】。纯函数，判定顺序固定。

    1 指标字段完整且不多不少        -> MISSING_METRICS
    2 指标为整数且在声明范围内      -> INVALID_METRICS
    3 医学安全门（仅运动类启用）：
        字段合法                   -> INVALID_MEDICAL
        疼痛 / 停止信号 / RPE 过高 -> PAIN_REPORTED / MEDICAL_STOP_SIGN / RPE_TOO_HIGH
    4 每个指标 >= min_valid        -> SKILL_GATE_FAILED
    5 通过：技能系数 = 10000 + 500 x 达到 good 线的指标数
    """
    if not isinstance(metrics, dict):
        return MetricVerdict(False, "MISSING_METRICS")
    if set(metrics) != set(rule.required_keys):
        return MetricVerdict(False, "MISSING_METRICS")

    checks: list[dict] = []
    for spec in rule.metrics:
        value = metrics[spec.key]
        if (isinstance(value, bool) or not isinstance(value, int)
                or not (spec.range_low <= value <= spec.range_high)):
            return MetricVerdict(False, "INVALID_METRICS")
        checks.append({
            "key": spec.key, "label": spec.label, "unit": spec.unit,
            "value": value, "min_valid": spec.min_valid, "good": spec.good,
            "in_range": True, "basis": spec.basis,
            "source_ids": list(spec.source_ids), "note": spec.note,
        })

    if rule.medical_gate:
        pain, rpe, stop = (metrics[k] for k in MEDICAL_KEYS)
        if (any(isinstance(v, bool) or not isinstance(v, int)
                for v in (pain, rpe, stop))
                or pain not in (0, 1) or stop not in (0, 1)
                or not (RPE_MIN <= rpe <= RPE_MAX)):
            return MetricVerdict(False, "INVALID_MEDICAL")
        med_checks = [
            {"key": "pain_flag", "label": "疼痛/不适", "unit": "0=无 1=有",
             "value": pain, "passed_gate": pain == 0, "at_good": False,
             "basis": "hard", "source_ids": ["stop_signs"]},
            {"key": "rpe", "label": "Borg 主观用力等级", "unit": "6-20",
             "value": rpe, "passed_gate": rpe <= RPE_REJECT_ABOVE, "at_good": rpe <= RPE_VIGOROUS,
             "basis": "hard", "source_ids": ["borg_rpe"]},
            {"key": "stop_sign", "label": "停止运动警示信号", "unit": "0=无 1=有",
             "value": stop, "passed_gate": stop == 0, "at_good": False,
             "basis": "hard", "source_ids": ["stop_signs", "acsm_screening"]},
        ]
        checks.extend(med_checks)
        if pain != 0:
            return MetricVerdict(False, "PAIN_REPORTED", checks=tuple(checks))
        if stop != 0:
            return MetricVerdict(False, "MEDICAL_STOP_SIGN", checks=tuple(checks))
        if rpe > RPE_REJECT_ABOVE:
            return MetricVerdict(False, "RPE_TOO_HIGH", checks=tuple(checks))

    good_count = 0
    for check, spec in zip(checks, rule.metrics):
        check["passed_gate"] = check["value"] >= spec.min_valid
        check["at_good"] = check["value"] >= spec.good
        if check["at_good"]:
            good_count += 1
    if not all(c["passed_gate"] for c in checks[:len(rule.metrics)]):
        return MetricVerdict(False, "SKILL_GATE_FAILED", checks=tuple(checks))
    factor = 10_000 + GOOD_BONUS_BPS * good_count
    return MetricVerdict(True, "OK", factor, tuple(checks))


def assess_skill_level(rule: SkillRule, level: int, stats: dict,
                       metric_sums: dict[str, int]) -> dict:
    """按技能专属标准逐项判定某等级是否达标。纯函数。

    指标项用「历史累计 / 有效练习次数」得到的平均值判定，
    比单次最好成绩更能反映稳定水平；所有除法为整数整除。
    """
    base = assess_level(level, stats)
    sessions = int(stats.get("valid_sessions", 0))
    index = min(max(level, 1), len(LEVEL_CRITERIA)) - 1
    enabled = bool(SHAPE_RULES["enabled"])
    wsum = window_summary(stats.get("recent_sessions")) if enabled else None
    use_window = bool(base["window_used"])
    fast = quick_pass(wsum["rows"], level, rule) if (enabled and wsum) else False

    extra = []
    lifetime = {}
    for spec in rule.metrics:
        life_avg = (int(metric_sums.get(spec.key, 0)) // sessions
                    if sessions > 0 else 0)
        lifetime[spec.key] = life_avg
        avg = (int(wsum["metric_avgs"].get(spec.key, 0))
               if use_window else life_avg)
        required = spec.level_requirements[index]
        extra.append({
            "key": spec.key,
            "label": (f"{spec.label}近期均值（{spec.unit}）" if use_window
                      else f"{spec.label}均值（{spec.unit}）"),
            "required": required,
            "actual": avg,
            "lifetime_avg": life_avg,
            "passed": avg >= required or fast,
            "action": (f"把{spec.label}均值从 {avg} 提到 {required} {spec.unit}"
                       f"（下次这项至少填 {required}）"
                       if avg < required else ""),
            "is_skill_metric": True,
        })
    checks = list(base["checks"]) + extra
    if fast:
        # 连续 N 次逐次达标：只免除均值类门槛，次数/反馈/难度/搁置照常。
        for check in checks:
            if check["key"] == "min_avg_quality" or check["key"] in {
                    spec.key for spec in rule.metrics}:
                check["passed"] = True
    kind = rule.mapping_kind(level)
    note = {
        "official": "",
        "self_defined": SELF_DEFINED_MAPPING,
        "unmapped": NO_OFFICIAL_MAPPING,
    }[kind]
    return {
        "level": level,
        "name": base["name"],
        "skill_id": rule.skill_id,
        "observable": rule.level_observables[index],
        "standard": rule.standard,
        "evidence_type": rule.evidence_type,
        "no_official_mapping": kind != "official",
        "mapping_kind": kind,
        "mapping_note": note,
        "checks": checks,
        "qualified": all(c["passed"] for c in checks),
        "window_used": use_window,
        "window_count": base["window_count"],
        "fast_track": fast,
        "lifetime_metric_avgs": lifetime,
    }


def rulebook() -> dict:
    """导出完整规则定义，供网页展示与人工核对。"""
    return {
        "constants": {
            "MIN_QUALITY": MIN_QUALITY,
            "NO_FEEDBACK_QUALITY_GATE": NO_FEEDBACK_QUALITY_GATE,
            "LOW_STREAK_LIMIT": LOW_STREAK_LIMIT,
            "REPEAT_WINDOW": REPEAT_WINDOW,
            "RESUME_IDLE_DAYS": RESUME_IDLE_DAYS,
            "DECAY_GRACE_SECONDS": DECAY_GRACE_SECONDS,
            "RETENTION_FLOOR_BPS": RETENTION_FLOOR_BPS,
            "GOOD_BONUS_BPS": GOOD_BONUS_BPS,
        },
        "max_level": MAX_LEVEL,
        "skill_ids": list(SKILL_IDS),
        "skill_rules": {sid: rule.to_dict() for sid, rule in SKILL_RULES.items()},
        "standard_sources": {k: dict(v) for k, v in STANDARD_SOURCES.items()},
        "decision_points": [dict(p) for p in DECISION_POINTS],
        "decision_status": "decided",
        "open_decisions": [dict(x) for x in OPEN_DECISIONS],
        "open_decision_status": "applied",
        "open_decision_evidence": collect_open_items(),
        "applied_decisions": dict(APPLIED_DECISIONS),
        "implementation_notes": dict(IMPLEMENTATION_NOTES),
        "assess_gate_enabled": True,
        "streak_rules": dict(STREAK_RULES),
        "article_fact_checks": [dict(x) for x in ARTICLE_FACT_CHECKS],
        "source_verification": [dict(x) for x in SOURCE_VERIFICATION],
        "diagnosis_verification": [dict(x) for x in DIAGNOSIS_VERIFICATION],
        "proficiency_definition": dict(PROFICIENCY_DEFINITION),
        "review_verification": [dict(x) for x in REVIEW_VERIFICATION],
        "shape_rules": dict(SHAPE_RULES),
        "trust_rules": dict(TRUST_RULES),
        "low_friction": dict(LOW_FRICTION_RULES),
        "settlement_form_meta": dict(SETTLEMENT_FORM_META),
        "settlement_forms": settlement_form_book(),
        "capability_verification": [dict(x) for x in CAPABILITY_VERIFICATION],
        "narrative_rules": dict(NARRATIVE_RULES),
        "altruism_rules": dict(ALTRUISM_RULES),
        "altruism_reason_codes": dict(ALTRUISM_REASON_CODES),
        "reputation_titles": [dict(x) for x in REPUTATION_TITLES],
        "ulives_reference": ULIVES_REFERENCE,
        "realm_titles": list(REALM_TITLES),
        "achievements": [dict(x) for x in ACHIEVEMENTS],
        "plain_rules": [dict(x) for x in PLAIN_RULES],
        "medical_gate": {
            "keys": list(MEDICAL_KEYS),
            "rpe_range": [RPE_MIN, RPE_MAX],
            "rpe_vigorous": RPE_VIGOROUS,
            "rpe_reject_above": RPE_REJECT_ABOVE,
            "source_ids": ["borg_rpe", "acsm_screening", "stop_signs"],
        },
        "skill_metric_gate_order": [
            "MISSING_METRICS", "INVALID_METRICS", "INVALID_MEDICAL",
            "PAIN_REPORTED", "MEDICAL_STOP_SIGN", "RPE_TOO_HIGH",
            "SKILL_GATE_FAILED", "OK",
        ],
        "threshold_disclaimer": (
            "等级锚点：语言采用 CEFR/DELE 官方 can-do（A1–C2），办公采用 ICDL/MOS 阶梯，"
            "剪辑 LV1–LV2 采用 Adobe ACP 官方指南，运动安全门采用 Borg RPE/ACSM；"
            "LV1–LV8 名称对齐 EQF 描述符。每个指标在 basis 中标明 hard(官方) / draft(文章示例草案，需拍板) / system(本系统标尺)。"
            "官方未覆盖的等级：按拍板结果，语言 LV7–LV8 与剪辑 LV3–LV8 自建标准并标注「自建标准·非官方」；"
            "办公 LV6–LV8 仍标注「无官方对应，待定」。两种情况都不冒充官方阈值。"
        ),
        "trigger_boundaries": {
            "upgrade": "结算后有效熟练度 >= 下一级累计阈值，且逐级通过 assess_skill_level 可观测达标校验才升级；未通过则卡在原级（gate_blocked=True），一次练习可连升多级",
            "gate_exempt": "add_exp / set_total_exp / set_level 属管理注入与回档，显式授予经验对应等级，绕过练习路径的达标校验",
            "downgrade": "结算后有效熟练度 < 当前等级累计阈值即退化；因 1 天宽限期，实际只在搁置 >= 1 天后发生；最低 Lv.1",
            "grace": "搁置不足 1 天：保持度恒为 10000 bps，不生疏、不退化，避免阈值边界反复跳级",
            "settle": "生疏与退化不隐式发生，只在 settle(now)/add_practice(now)/带 now 的查询时结算",
            "floor": "保持度下限 3500 bps，历史累计经验单调不减，等级不会退到 Lv.1 以下",
            "resume": "搁置 >= 14 天后首次有效练习标记为中断后恢复，回补保持度使等级快速回升",
        },
        "feedback_levels": FEEDBACK_LEVELS,
        "feedback_factor_bps": FEEDBACK_FACTOR_BPS,
        "challenge_factor_bps": CHALLENGE_FACTOR_BPS,
        "repeat_factor_bps": REPEAT_FACTOR_BPS,
        "decay_anchors": [{"days": d, "retention_bps": b} for d, b in DECAY_ANCHORS],
        "reason_codes": REASON_CODES,
        "level_criteria": [dict(c) for c in LEVEL_CRITERIA],
        "formula": {
            "gain": "max(1, round(20*D * Q/100 * F * M * R * S))，S=10000+500*达到good线的指标数（bps）",
            "cost": "ceil(base * L^k), k>1（默认 1.5）",
            "effective_exp": "total_exp * retention_bps // 10000",
            "retention": "min(上次保持度, 分段线性锚表(搁置时长)), 下限 3500 bps",
            "recovery": "min(10000, 400 + 6*Q + 100*(有效挑战) + 200*(中断恢复))",
        },
    }
