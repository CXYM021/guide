"""升级验证题（level-up checkpoint）。

只有「可判分」的语言类技能有这个题库：它们的等级本来就有外部标准可对照，
用三道客观题比再填一次自评更可信。动作类与软件类技能不走这里——它们的
证据是成绩、仪器读数与交付物，选几道选择题反而更弱。

规则：
  - 目标等级 ≥ QUIZ_MIN_LEVEL 才要求验证（LV5 起），LV1–LV4 不受影响；
  - 每次只出 3 题，题目在题库里轮换，不会连着出同一套；
  - 答错就关掉，等级与经验都不变，直到下一次有效练习后才再出题；
  - 答对的题累计，累计满 3 题才升到这一级（答错不扣、也不清零）；
  - 通过后记 verified_level，下一次升级再验一次。
纯数据 + 纯函数，没有 IO。
"""

from __future__ import annotations

QUIZ_MIN_LEVEL = 5
QUIZ_CREDITS_TO_PASS = 3      # 累计答对 3 题才算过
QUIZ_ITEMS_PER_ATTEMPT = 3    # 每次只出 3 题
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
            {
                "id": "rw4",
                "stem": "It is essential that every applicant ___ the form before Friday.",
                "options": ("submits", "submit", "submitted", "will submit"),
                "answer": 1,
                "why": "It is essential that 后接虚拟语气，动词用原形 submit。",
            },
            {
                "id": "rw5",
                "stem": "The number of applicants ___ risen sharply this year.",
                "options": ("have", "has", "were", "are"),
                "answer": 1,
                "why": "主语中心词是 the number（单数），谓语用 has；a number of 才跟复数。",
            },
            {
                "id": "rw6",
                "stem": "___ hard he tried, he could not meet the deadline.",
                "options": ("However", "Whatever", "Whenever", "Wherever"),
                "answer": 0,
                "why": "However + 形容词/副词 + 主语 + 谓语，表示「无论多么…」。",
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
            {
                "id": "sp4",
                "stem": "连读里最自然的是哪一句？",
                "options": ("an apple → /ən ˈæpl/（n 与 a 连上）",
                            "an apple → 两个词之间明显停顿",
                            "an apple → 把 an 读成 /eɪn/",
                            "an apple → 重读 an"),
                "answer": 0,
                "why": "辅音结尾 + 元音开头要连读，an apple 听起来像 a-napple。",
            },
            {
                "id": "sp5",
                "stem": "句子的信息重音通常落在哪里？",
                "options": ("每个词都重读", "最后一个实词或对比焦点",
                            "所有虚词上", "句首第一个词"),
                "answer": 1,
                "why": "英语是重音计时语言，核心信息落在最后实词或对比焦点，虚词弱读。",
            },
            {
                "id": "sp6",
                "stem": "comfortable 日常口语里通常读几个音节？",
                "options": ("3 个（COMF-ta-ble）", "4 个（com-for-ta-ble）",
                            "5 个", "2 个"),
                "answer": 0,
                "why": "口语里常压缩成 3 个音节 /ˈkʌmftəbl/，写出来是 4 个音节。",
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
            {
                "id": "es4",
                "stem": "María ___ en Madrid desde 2019.",
                "options": ("vive", "está viviendo", "vivió", "vivía"),
                "answer": 0,
                "why": "desde + 时间点表示从过去持续到现在，用现在时 vive。",
            },
            {
                "id": "es5",
                "stem": "Este regalo es ___ ti.",
                "options": ("por", "para", "de", "en"),
                "answer": 1,
                "why": "para 表示目的地/受益者；por 多表示原因、交换、经由。",
            },
            {
                "id": "es6",
                "stem": "Cuando era niño, ___ al parque todos los días.",
                "options": ("fui", "iba", "voy", "iré"),
                "answer": 1,
                "why": "过去的习惯性动作用未完成过去时 iba。",
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
        "credits_needed": QUIZ_CREDITS_TO_PASS,
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

def pick(skill_id: str, seen=(), limit: int = QUIZ_ITEMS_PER_ATTEMPT) -> list[dict]:
    """挑这一轮要出的题：优先没见过的，题库轮完再从头开始。不含答案。"""
    bank = _BANKS.get(skill_id)
    if bank is None:
        return []
    used = [str(x) for x in (seen or [])]
    items = list(bank["items"])
    fresh = [it for it in items if it["id"] not in used]
    if len(fresh) < limit:
        # 题库轮完了：允许重复，但把「刚答对的」放到最后
        fresh = fresh + [it for it in items if it["id"] in used]
    return [{"id": it["id"], "stem": it["stem"], "options": list(it["options"])}
            for it in fresh[:limit]]


def grade_answers(skill_id: str, answers: dict) -> dict:
    """判这一轮的题：返回正确数与逐题结果（含解析），不判定是否升级。"""
    bank = _BANKS.get(skill_id)
    if bank is None:
        raise ValueError("该技能没有等级验证题")
    if not isinstance(answers, dict):
        raise ValueError("答案必须是对象")
    by_id = {it["id"]: it for it in bank["items"]}
    detail, right = [], []
    for qid, raw in answers.items():
        it = by_id.get(str(qid))
        if it is None:
            continue
        ok = isinstance(raw, int) and not isinstance(raw, bool) and raw == it["answer"]
        if ok:
            right.append(it["id"])
        detail.append({"id": it["id"], "correct": ok,
                       "answer": it["answer"], "why": it["why"]})
    return {"ok": True, "skill_id": skill_id, "correct": len(right),
            "answered": len(detail), "correct_ids": right, "detail": detail,
            "framework": bank["framework"]}
