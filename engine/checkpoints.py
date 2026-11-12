"""升级验证题（level-up checkpoint）。

只有「可判分」的语言类技能有这个题库：它们的等级本来就有外部标准可对照，
用三道客观题比再填一次自评更可信。动作类与软件类技能不走这里——它们的
证据是成绩、仪器读数与交付物，选几道选择题反而更弱。

规则：
  - 目标等级 ≥ QUIZ_MIN_LEVEL 才要求验证（LV5 起），LV1–LV4 不受影响；
  - 三道题全对才算过，答错可以重做，不扣经验、不写负分；
  - 通过后记 verified_level，下一次升级再验一次。
纯数据 + 纯函数，没有 IO。
"""

from __future__ import annotations

QUIZ_MIN_LEVEL = 5
PASS_RATIO_NUM, PASS_RATIO_DEN = 3, 3

_BANKS: dict[str, dict] = {
    "英语读写": {
        "framework": "CET-6 / CEFR B2–C1 语法与搭配",
        "items": (
            {
                "id": "rw1",
                "stem": "No sooner ___ the announcement than the phones started ringing.",
                "options": ("has", "had", "have", "having"),
                "answer": 1,
                "why": "No sooner 放句首要部分倒装，主句用过去完成时：had … than …。",
            },
            {
                "id": "rw2",
                "stem": "The report, ___ findings were disputed, was withdrawn.",
                "options": ("whose", "which", "of which", "what"),
                "answer": 0,
                "why": "whose 在这里指 the report's findings，是最直接的所属关系。",
            },
            {
                "id": "rw3",
                "stem": "The two explanations are mutually ___ ; only one can be true.",
                "options": ("exclusive", "exclusion", "excluded", "excluding"),
                "answer": 0,
                "why": "mutually exclusive 是固定搭配，表示「互斥」。",
            },
        ),
    },
    "英语口语": {
        "framework": "口语语音与连读常识（CEFR B2 以上）",
        "items": (
            {
                "id": "sp1",
                "stem": "photography 的重音在第几个音节？",
                "options": ("第 1 个", "第 2 个", "第 3 个", "第 4 个"),
                "answer": 1,
                "why": "pho-TOG-ra-phy，重音在第二音节；同族的 photograph 才在第一音节。",
            },
            {
                "id": "sp2",
                "stem": "economic 的重音在哪个音节？",
                "options": ("第 1 个", "第 2 个", "第 3 个", "第 4 个"),
                "answer": 2,
                "why": "e-co-NOM-ic，重音在第三音节；经济和「节约」的 economical 重音位置不同。",
            },
            {
                "id": "sp3",
                "stem": "自然语流里，I want to go 通常读成？",
                "options": ("I want to go（逐词重读）",
                            "I wanna go（want to 连读弱化）",
                            "I wan't go",
                            "I want-to-go（三个都重读）"),
                "answer": 1,
                "why": "want to 在口语里弱化为 wanna，重音落在 want 与 go 上。",
            },
        ),
    },
    "西班牙语": {
        "framework": "DELE A2–B1 基础语法",
        "items": (
            {
                "id": "es1",
                "stem": "Yo ___ español todos los días.",
                "options": ("hablo", "hablas", "habla", "hablamos"),
                "answer": 0,
                "why": "主语 Yo 对应第一人称单数 hablo。",
            },
            {
                "id": "es2",
                "stem": "Ayer ___ al cine con mis amigos.",
                "options": ("voy", "fui", "iré", "iba"),
                "answer": 1,
                "why": "Ayer 指过去的一次完成动作，用简单过去时 fui。",
            },
            {
                "id": "es3",
                "stem": "Me gusta ___ música mientras trabajo.",
                "options": ("escuchar", "escucho", "escucha", "escuchando"),
                "answer": 0,
                "why": "gustar 后接不定式作主语：me gusta escuchar…。",
            },
        ),
    },
}


def has_bank(skill_id: str) -> bool:
    return skill_id in _BANKS


def quiz_level(skill_id: str) -> int | None:
    """需要验证的最低目标等级；没有题库返回 None。"""
    return QUIZ_MIN_LEVEL if has_bank(skill_id) else None


def spec(skill_id: str) -> dict | None:
    """给前端的题面（不含答案）。"""
    bank = _BANKS.get(skill_id)
    if bank is None:
        return None
    return {
        "skill_id": skill_id,
        "framework": bank["framework"],
        "min_level": QUIZ_MIN_LEVEL,
        "pass_needed": PASS_RATIO_NUM,
        "items": [{"id": it["id"], "stem": it["stem"],
                   "options": list(it["options"])} for it in bank["items"]],
    }


def grade(skill_id: str, answers: dict) -> dict:
    """判分。answers: {item_id: 选项下标}，缺项按错处理。"""
    bank = _BANKS.get(skill_id)
    if bank is None:
        raise ValueError("该技能没有等级验证题")
    if not isinstance(answers, dict):
        raise ValueError("答案必须是对象")
    correct, detail = 0, []
    for it in bank["items"]:
        raw = answers.get(it["id"])
        ok = isinstance(raw, int) and not isinstance(raw, bool) and raw == it["answer"]
        if ok:
            correct += 1
        detail.append({
            "id": it["id"],
            "correct": ok,
            "answered": raw if isinstance(raw, int) else None,
            "answer": it["answer"],
            "why": it["why"],
        })
    passed = correct >= PASS_RATIO_NUM
    return {
        "ok": True,
        "skill_id": skill_id,
        "correct": correct,
        "total": len(bank["items"]),
        "passed": passed,
        "detail": detail,
        "framework": bank["framework"],
    }
