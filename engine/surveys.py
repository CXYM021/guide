"""两张问卷的判定口径（纯函数，无 IO、无随机）。

边界（与引擎其它规则一致，改这里之前先读这三条）：
1. 问卷只产出「等级上限」或「经验系数」，从不直接写 total_exp。
   首登问卷给的是「已会的信用等级」，不是刷出来的经验。
2. 自评起点最高 LV3，且只在 total_exp == 0、没有练习事件时才能设置。
   没有证据的自我报告不该换来更高的等级。
3. 每次练习的问卷系数锁定在 0.80–1.12，四项相乘后截断。
   它只能微调，不能盖过难度 × 质量 × 反馈的主判定。

问卷本身不入经验账本，答案随事件写进 note / survey 字段，供事后核对。
"""

from __future__ import annotations

BASELINE_MAX_LEVEL = 3

# 自评起点：同一档只能选一个；points 相加决定等级，不做加权平均以外的解释。
BASELINE_QUESTIONS = (
    {
        "key": "years",
        "label": "这门技能，你认真练了多久？",
        "options": (
            {"value": "none", "label": "没练过，从零开始", "points": 0},
            {"value": "lt1", "label": "不到 1 年", "points": 1},
            {"value": "1to3", "label": "1–3 年", "points": 2},
            {"value": "gt3", "label": "3 年以上", "points": 3},
        ),
    },
    {
        "key": "frequency",
        "label": "过去半年，你多久练一次？",
        "options": (
            {"value": "rare", "label": "几乎不练", "points": 0},
            {"value": "weekly", "label": "每周 1–2 次", "points": 1},
            {"value": "often", "label": "每周 3 次以上", "points": 2},
            {"value": "daily", "label": "几乎每天", "points": 3},
        ),
    },
    {
        "key": "feedback",
        "label": "你收到过什么样的反馈？",
        "options": (
            {"value": "none", "label": "没有，全靠自己感觉", "points": 0},
            {"value": "self", "label": "自己录音 / 录像回看", "points": 1},
            {"value": "peer", "label": "同伴或老师点评", "points": 2},
            {"value": "measured", "label": "有客观成绩、教练或比赛", "points": 3},
        ),
    },
    {
        "key": "transfer",
        "label": "在真实场合用过这门技能吗？",
        "options": (
            {"value": "never", "label": "没有", "points": 0},
            {"value": "sometimes", "label": "偶尔", "points": 1},
            {"value": "often", "label": "经常", "points": 2},
        ),
    },
)

# 每次练习的微调项：都回答「这一次」，不是「我一贯」。
SESSION_QUESTIONS = (
    {
        "key": "focus",
        "label": "这次练得专注吗？",
        "options": (
            {"value": "scattered", "label": "经常走神", "bps": 9500},
            {"value": "normal", "label": "一般", "bps": 10000},
            {"value": "deep", "label": "很投入", "bps": 10500},
        ),
    },
    {
        "key": "fatigue",
        "label": "练的时候精力如何？",
        "options": (
            {"value": "fresh", "label": "精力充沛", "bps": 10300},
            {"value": "normal", "label": "正常", "bps": 10000},
            {"value": "tired", "label": "有点累", "bps": 9600},
            {"value": "spent", "label": "很疲惫", "bps": 9000},
        ),
    },
    {
        "key": "novelty",
        "label": "这次练的是新内容还是旧内容？",
        "options": (
            {"value": "review", "label": "复习已经会的", "bps": 9600},
            {"value": "mixed", "label": "新旧混合", "bps": 10000},
            {"value": "new", "label": "新内容 / 新难度", "bps": 10400},
        ),
    },
    {
        "key": "completion",
        "label": "计划的任务完成到哪一步？",
        "options": (
            {"value": "partial", "label": "只做了一小部分", "bps": 9200},
            {"value": "most", "label": "大部分完成", "bps": 9800},
            {"value": "done", "label": "按计划完成", "bps": 10200},
            {"value": "extra", "label": "超额完成", "bps": 10500},
        ),
    },
)

SESSION_MIN_BPS = 8000
SESSION_MAX_BPS = 11200


def _questions(spec):
    """把内部元组转成前端可直接渲染的结构（深拷贝，防止调用方改到常量）。"""
    out = []
    for q in spec:
        out.append({
            "key": q["key"],
            "label": q["label"],
            "options": [dict(o) for o in q["options"]],
        })
    return out


def baseline_questions() -> list[dict]:
    return _questions(BASELINE_QUESTIONS)


def session_questions() -> list[dict]:
    return _questions(SESSION_QUESTIONS)


def _pick(spec, answers: dict, *, kind: str) -> tuple[dict, list[dict]]:
    """按 spec 校验答案，返回 (选项, 逐项明细)。缺项或未知取值都拒绝。"""
    if answers is None:
        answers = {}
    if not isinstance(answers, dict):
        raise ValueError(f"{kind}答案必须是一个对象")
    chosen, detail = [], []
    for q in spec:
        key = q["key"]
        if key not in answers:
            raise ValueError(f"{kind}问卷缺少「{q['label']}」")
        value = answers[key]
        opt = next((o for o in q["options"] if o["value"] == value), None)
        if opt is None:
            raise ValueError(f"{kind}问卷「{q['label']}」的取值无效：{value!r}")
        chosen.append(opt)
        detail.append({"key": key, "value": value, "label": opt["label"]})
    return chosen, detail


def baseline_level(answers: dict) -> dict:
    """自评 → 起始等级（1..3）。分数越高只代表「更像已经会的人」。"""
    chosen, detail = _pick(BASELINE_QUESTIONS, answers, kind="自评")
    points = sum(o["points"] for o in chosen)
    # 满分 11：9 分意味着「1–3 年 + 每周 3 次 + 有客观成绩 + 真实场合用过」，
    # 这是能拿 LV3 信用的最低组合；再往上没有更高的起点。
    if points >= 9:
        level = 3
    elif points >= 5:
        level = 2
    else:
        level = 1
    level = max(1, min(BASELINE_MAX_LEVEL, level))
    return {
        "level": level,
        "points": points,
        "max_level": BASELINE_MAX_LEVEL,
        "detail": detail,
        "why": {
            1: "从零开始：起点就是 LV1，经验和门槛都按正常路径走。",
            2: "有底子：给你 LV2 的信用，但从 LV3 起仍要经验与达标校验。",
            3: "已入门：最高只给到 LV3，且不再往上加；LV4 起必须靠练。",
        }[level],
    }


def session_factor_bps(answers: dict) -> dict:
    """这一次练习的微调系数：四项连乘后截断到 8000–11200 基点。"""
    chosen, detail = _pick(SESSION_QUESTIONS, answers, kind="练习")
    bps = 10_000
    for o in chosen:
        bps = bps * int(o["bps"]) // 10_000
    clamped = max(SESSION_MIN_BPS, min(SESSION_MAX_BPS, bps))
    return {
        "bps": clamped,
        "raw_bps": bps,
        "clamped": clamped != bps,
        "multiplier": round(clamped / 10_000, 3),
        "detail": detail,
    }
