"""两张问卷的判定口径（纯函数，无 IO、无随机）。

边界（与引擎其它规则一致，改这里之前先读这三条）：
1. 问卷只产出「等级上限」或「经验系数」，从不直接写 total_exp。
   首登问卷给的是「已会的信用等级」，不是刷出来的经验。
2. 首登自评按技能各用一套有外部锚点的题（CEFR / DELE / 比赛成绩 / 相对力量 /
   作品与交付），最高给到 LV6；LV7–8 必须靠复测与客观成绩，自评不给。
3. 每次练习的问卷系数锁定在 0.80–1.12，四项相乘后截断。
   它只能微调，不能盖过难度 × 质量 × 反馈的主判定。

问卷本身不入经验账本，答案随事件写进 note / survey 字段，供事后核对。
"""

from __future__ import annotations

# 自评能拿到的最高等级：再往上要靠复测与客观成绩。
BASELINE_MAX_LEVEL = 6


def _o(value: str, label: str, points: int) -> dict:
    return {"value": value, "label": label, "points": points}


def _q(key: str, label: str, options: tuple) -> dict:
    return {"key": key, "label": label, "options": options}


# ---------------------------------------------------------------- 分技能题库
#
# 每套题只问「可以对照外部标准核对的事实」，不问「你觉得自己怎么样」：
# 考试分数带、证书等级、比赛成绩、相对体重、作品类型与交付量。
# 同一题里 points 的档位差，就是「离下一级还差哪一块」的提示。

_SPEAKING = {
    "framework": "CEFR 口语 can-do 描述 + IELTS Speaking / TOEFL Speaking 分数带",
    "max_level": 6,
    "questions": (
        _q("cefr", "按 CEFR 的口语描述，你更接近哪一档？", (
            _o("a1", "A1：能介绍自己、点餐购物，句子短、常停顿", 4),
            _o("a2", "A2：能应付日常对话，复杂话题会卡住", 10),
            _o("b1", "B1：能讲清经历与计划，语速慢但能连贯说一段", 18),
            _o("b2", "B2：能参与讨论、表达观点，卡顿不影响交流", 28),
            _o("c1", "C1：工作或学术话题能流畅展开，很少想词", 36),
            _o("c2", "C2：接近母语者的自如，能处理抽象话题与幽默", 40),
        )),
        _q("exam", "最近一次英语口语考试（IELTS / TOEFL / 其他）", (
            _o("none", "没考过口语", 0),
            _o("low", "IELTS 5.5 以下 / TOEFL 15 以下", 6),
            _o("mid", "IELTS 6–6.5 / TOEFL 18–22", 12),
            _o("high", "IELTS 7–7.5 / TOEFL 23–27", 18),
            _o("top", "IELTS 8 以上 / TOEFL 28 以上", 24),
        )),
        _q("sustain", "不准备，就一个抽象话题能连续说多久？", (
            _o("no", "说不满 30 秒", 0),
            _o("30s", "30 秒左右", 4),
            _o("1m", "1 分钟左右", 8),
            _o("2m", "2 分钟以上", 12),
        )),
        _q("use", "真实场合里用过英语口语吗？", (
            _o("none", "几乎没用过", 0),
            _o("travel", "旅行、点餐、问路", 4),
            _o("work", "工作会议发言、和外国同事日常沟通", 9),
            _o("lead", "主讲汇报、谈判、教学或主持", 14),
        )),
        _q("listener", "对方听懂你的程度？", (
            _o("repeat", "经常要请我重复", 0),
            _o("accent", "有口音，但基本能听懂", 4),
            _o("fine", "沟通基本没有障碍", 7),
            _o("native", "常被当成在海外生活多年的人", 10),
        )),
    ),
}

_READWRITE = {
    "framework": "CEFR 读写 can-do 描述 + IELTS Reading/Writing 分数带",
    "max_level": 6,
    "questions": (
        _q("read", "按 CEFR 的阅读描述，你更接近哪一档？", (
            _o("a1", "A1–A2：短消息、菜单、通知", 4),
            _o("b1", "B1：新闻标题与日常邮件", 12),
            _o("b2", "B2：小说、长报道、公司文档", 22),
            _o("c1", "C1：专业文献、行业报告", 32),
            _o("c2", "C2：学术论文、文学与非虚构长文", 40),
        )),
        _q("write", "按 CEFR 的写作描述，你更接近哪一档？", (
            _o("a1", "A1–A2：短句、简单留言", 4),
            _o("b1", "B1：日常邮件、短文", 12),
            _o("b2", "B2：工作报告、说明文档", 20),
            _o("c1", "C1：学术写作、正式公文", 28),
        )),
        _q("exam", "最近一次英语读写考试（IELTS / TOEFL / CET）", (
            _o("none", "没考过", 0),
            _o("low", "IELTS 5.5 以下 / TOEFL 15 以下 / CET-4 未过", 5),
            _o("cet4", "CET-4 已过、CET-6 还没过", 7),
            _o("mid", "IELTS 6–6.5 / TOEFL 18–22 / CET-6 通过", 10),
            _o("high", "IELTS 7–7.5 / TOEFL 23–27", 15),
            _o("top", "IELTS 8 以上 / TOEFL 28 以上", 20),
        )),
        _q("volume", "每周读多少英文长文（新闻、文档、书）？", (
            _o("little", "不到 1,000 词", 2),
            _o("some", "1,000–3,000 词", 5),
            _o("much", "3,000–10,000 词", 8),
            _o("lots", "10,000 词以上", 12),
        )),
        _q("output", "写过的最正式的英文文本是什么？", (
            _o("none", "没写过", 0),
            _o("mail", "邮件、社交媒体", 4),
            _o("report", "报告、方案、对外文档", 8),
            _o("paper", "论文、发表、出版", 12),
        )),
    ),
}

_SPANISH = {
    "framework": "DELE（塞万提斯学院）A1–C2 等级 + 真实语速听力",
    "max_level": 6,
    "questions": (
        _q("dele", "你现在的西班牙语大概在 DELE 哪一档？", (
            _o("a1", "A1：能应付问候、点餐、自我介绍", 5),
            _o("a2", "A2：能完成日常事务对话", 12),
            _o("b1", "B1：能讲清经历、处理旅行中的意外", 20),
            _o("b2", "B2：能参与讨论、看懂新闻", 30),
            _o("c1", "C1：工作或学术场景能流畅表达", 36),
            _o("c2", "C2：接近母语者，能读文学与专业文本", 40),
        )),
        _q("listening", "原速的西语内容能听懂多少？", (
            _o("none", "基本听不懂", 0),
            _o("subs", "带字幕能跟上", 6),
            _o("film", "无字幕电影能懂大意", 12),
            _o("podcast", "播客、新闻、访谈基本无障碍", 20),
        )),
        _q("immersion", "在西班牙语环境里生活或工作过吗？", (
            _o("never", "没有", 0),
            _o("travel", "旅游过", 4),
            _o("short", "短期生活或交换", 9),
            _o("work", "长期工作、留学或定居", 16),
        )),
        _q("years", "认真学这门语言多久了？", (
            _o("none", "没系统学过", 0),
            _o("lt1", "不到 1 年", 4),
            _o("1to3", "1–3 年", 8),
            _o("gt3", "3 年以上", 12),
        )),
        _q("use", "用它做过什么真实的事？", (
            _o("none", "没有", 0),
            _o("travel", "点餐、问路、闲聊", 4),
            _o("work", "工作沟通、写邮件", 8),
            _o("pro", "谈判、写作、教学或翻译", 12),
        )),
    ),
}

_RUNNING = {
    "framework": "Daniels VDOT 成绩表 + ACSM 跑量与强度原则",
    "max_level": 6,
    "questions": (
        _q("mileage", "现在每周跑多少公里？", (
            _o("none", "几乎不跑", 0),
            _o("lt10", "10 公里以下", 6),
            _o("10to30", "10–30 公里", 14),
            _o("30to50", "30–50 公里", 20),
            _o("50to80", "50–80 公里", 25),
            _o("gt80", "80 公里以上", 28),
        )),
        _q("fivek", "5 公里最好成绩（按比赛或计时跑）", (
            _o("none", "没测过", 0),
            _o("slow", "30 分钟以外", 6),
            _o("mid", "25–30 分钟", 12),
            _o("good", "20–25 分钟", 18),
            _o("fast", "17–20 分钟", 22),
            _o("elite", "17 分钟以内", 24),
        )),
        _q("long", "长距离比赛成绩", (
            _o("none", "没跑过", 0),
            _o("finish", "跑完过半马", 6),
            _o("sub2", "半马 2 小时以内", 12),
            _o("sub145", "半马 1 小时 45 分以内", 17),
            _o("sub330", "全马 3 小时 30 分以内", 20),
            _o("sub3", "全马 3 小时以内", 22),
        )),
        _q("plan", "训练是怎么安排的？", (
            _o("none", "想起来就跑", 0),
            _o("weekly", "有每周计划", 6),
            _o("cycle", "有周期化：间歇、长距离、力量都排", 12),
            _o("coach", "有教练或跑团跟练", 16),
        )),
        _q("technique", "跑步技术和伤病情况？", (
            _o("hurt", "反复受伤，没系统处理过", 0),
            _o("some", "偶尔膝踝不适，会自己调整", 4),
            _o("ok", "基本无伤，知道自己的动作问题", 7),
            _o("teach", "能看出别人的动作问题并给建议", 10),
        )),
    ),
}

_FITNESS = {
    "framework": "NSCA / ACSM 相对力量标准（三大项 ÷ 体重）+ 动作质量",
    "max_level": 6,
    "questions": (
        _q("years", "系统做力量训练多久了？", (
            _o("none", "没练过", 0),
            _o("lt1", "不到 1 年", 5),
            _o("1to3", "1–3 年", 10),
            _o("3to5", "3–5 年", 14),
            _o("gt5", "5 年以上", 18),
        )),
        _q("squat", "深蹲最大重量 ÷ 体重", (
            _o("none", "没练过深蹲", 0),
            _o("lt075", "0.75 倍以下", 6),
            _o("1x", "0.75–1.0 倍", 12),
            _o("15x", "1.0–1.5 倍", 17),
            _o("2x", "1.5–2.0 倍", 20),
            _o("gt2", "2.0 倍以上", 22),
        )),
        _q("bench", "卧推最大重量 ÷ 体重", (
            _o("none", "没练过卧推", 0),
            _o("lt05", "0.5 倍以下", 5),
            _o("075x", "0.5–0.75 倍", 11),
            _o("1x", "0.75–1.0 倍", 16),
            _o("gt1", "1.0 倍以上", 20),
        )),
        _q("deadlift", "硬拉最大重量 ÷ 体重", (
            _o("none", "没练过硬拉", 0),
            _o("lt1", "1.0 倍以下", 6),
            _o("15x", "1.0–1.5 倍", 12),
            _o("2x", "1.5–2.0 倍", 17),
            _o("25x", "2.0–2.5 倍", 20),
            _o("gt25", "2.5 倍以上", 22),
        )),
        _q("quality", "动作质量是谁在看？", (
            _o("none", "没人看过，自己练", 0),
            _o("video", "自己拍视频对照", 5),
            _o("peer", "有人（教练/老手）纠正过", 10),
            _o("coach", "长期有教练跟", 14),
            _o("compete", "参加过比赛或教别人", 18),
        )),
    ),
}

_OFFICE = {
    "framework": "MOS（Microsoft Office Specialist）能力阶梯 + 数据处理复杂度",
    "max_level": 6,
    "questions": (
        _q("excel", "Excel 用到什么程度？", (
            _o("input", "只会录入、简单求和", 0),
            _o("formula", "常用函数：VLOOKUP/IF/SUMIFS", 8),
            _o("pivot", "透视表、Power Query、条件格式", 16),
            _o("model", "复杂模型、多表关联、宏", 24),
            _o("auto", "自动化与开发：VBA/Python/接口", 30),
        )),
        _q("office", "Word / PowerPoint 做到什么程度？", (
            _o("basic", "能用模板写出来", 4),
            _o("format", "会排版、样式、图表", 10),
            _o("long", "长文档、母版、批量处理", 15),
            _o("deliver", "对外交付级：标书、年报、发布会", 20),
        )),
        _q("automation", "把重复工作自动化过吗？", (
            _o("none", "没有", 0),
            _o("macro", "录制过宏", 6),
            _o("vba", "写过 VBA", 12),
            _o("script", "用 Python / 脚本处理表格", 18),
            _o("tool", "给团队做过小工具或模板库", 24),
        )),
        _q("scale", "处理过最大规模的数据？", (
            _o("small", "几千行", 3),
            _o("mid", "十万行级", 7),
            _o("join", "多表关联、跨文件汇总", 11),
            _o("warehouse", "数据仓库、接口取数", 14),
        )),
        _q("influence", "别人怎么用你的 Office 能力？", (
            _o("none", "只服务自己", 0),
            _o("cert", "有 MOS 等证书", 4),
            _o("ask", "同事常来问怎么弄", 8),
            _o("teach", "给团队培训或定规范", 12),
        )),
    ),
}

_EDITING = {
    "framework": "作品类型与交付链（短视频/商业片/纪录片/院线）+ NLE 全流程",
    "max_level": 6,
    "questions": (
        _q("work", "你剪过最正式的作品是什么？", (
            _o("none", "没剪过", 0),
            _o("vlog", "生活 vlog、旅行记录", 8),
            _o("short", "短视频、口播、带货", 15),
            _o("commercial", "商业宣传片、广告、MV", 23),
            _o("broadcast", "纪录片、综艺、院线或电视播出", 30),
        )),
        _q("flow", "一条片子的流程你能独立做到哪一步？", (
            _o("cut", "只会拼接、加字幕", 4),
            _o("edit", "剪辑 + 字幕 + 基础音效", 10),
            _o("grade", "再加调色与声音处理", 16),
            _o("full", "全流程独立交付（含包装）", 20),
            _o("team", "带团队或定流程", 22),
        )),
        _q("tools", "软件掌握（按你最强的一项选，不必全都用过）", (
            _o("capcut", "剪映 / 必剪类就够用", 4),
            _o("premiere", "Premiere 或 Final Cut 能独立剪", 8),
            _o("resolve", "达芬奇能剪能调色（没用过 PR / FCP 也算）", 12),
            _o("ae", "After Effects 合成与动效", 14),
            _o("multi", "多软件协同，按项目选工具", 16),
        )),
        _q("delivery", "作品交付与客户情况", (
            _o("hobby", "自己玩，没人付钱", 0),
            _o("friend", "朋友单、偶尔接活", 6),
            _o("paid", "稳定商单", 12),
            _o("retainer", "长期客户或平台签约", 17),
            _o("award", "播出、获奖或行业内认可", 20),
        )),
        _q("feedback", "作品的外部反馈", (
            _o("none", "几乎没人看", 0),
            _o("hundreds", "几百播放", 3),
            _o("viral", "十万以上播放或客户复购", 6),
            _o("pro", "客户指定要你剪 / 平台推荐", 9),
            _o("industry", "同行认可、被当作案例", 12),
        )),
    ),
}

SKILL_BASELINES = {
    "英语口语": _SPEAKING,
    "英语读写": _READWRITE,
    "西班牙语": _SPANISH,
    "跑步技术": _RUNNING,
    "健身技术": _FITNESS,
    "电脑办公软件技术": _OFFICE,
    "剪辑技术": _EDITING,
}

# 自建技能没有专属题库，退回通用四问，且最高只给 LV3。
GENERIC_BASELINE = {
    "framework": "通用自评（自建技能没有外部标准可对照）",
    "max_level": 3,
    # 通用题沿用旧口径：满分 11，≥9 → LV3，≥5 → LV2。
    "cuts": ((9 / 11, 3), (5 / 11, 2)),
    "questions": (
        _q("years", "这门技能，你认真练了多久？", (
            _o("none", "没练过，从零开始", 0),
            _o("lt1", "不到 1 年", 1),
            _o("1to3", "1–3 年", 2),
            _o("gt3", "3 年以上", 3),
        )),
        _q("frequency", "过去半年，你多久练一次？", (
            _o("rare", "几乎不练", 0),
            _o("weekly", "每周 1–2 次", 1),
            _o("often", "每周 3 次以上", 2),
            _o("daily", "几乎每天", 3),
        )),
        _q("feedback", "你收到过什么样的反馈？", (
            _o("none", "没有，全靠自己感觉", 0),
            _o("self", "自己录音 / 录像回看", 1),
            _o("peer", "同伴或老师点评", 2),
            _o("measured", "有客观成绩、教练或比赛", 3),
        )),
        _q("transfer", "在真实场合用过这门技能吗？", (
            _o("never", "没有", 0),
            _o("sometimes", "偶尔", 1),
            _o("often", "经常", 2),
        )),
    ),
}

# 比例阈值：得分率 → 等级。6 级还要求「至少 3 题落在该题最高档附近」，
# 防止「一题拉满、其余空着」也能拿到最高自评。
_LEVEL_CUTS = ((0.85, 6), (0.76, 5), (0.58, 4), (0.38, 3), (0.20, 2))
_TOP_ANCHORS_FOR_MAX = 3


def _spec(skill_id: str | None) -> dict:
    return SKILL_BASELINES.get(skill_id or "", GENERIC_BASELINE)


def _questions(spec) -> list[dict]:
    """把内部元组转成前端可直接渲染的结构（深拷贝，防止调用方改到常量）。"""
    out = []
    for q in spec["questions"]:
        out.append({
            "key": q["key"],
            "label": q["label"],
            "options": [dict(o) for o in q["options"]],
        })
    return out


def baseline_questions(skill_id: str | None = None) -> list[dict]:
    return _questions(_spec(skill_id))


def baseline_specs() -> dict:
    """前端首登问卷要用的全部题库：键是技能 id，值是题面与所依标准。"""
    out = {}
    for skill_id, spec in SKILL_BASELINES.items():
        out[skill_id] = {
            "framework": spec["framework"],
            "max_level": spec["max_level"],
            "questions": _questions(spec),
        }
    out["__default__"] = {
        "framework": GENERIC_BASELINE["framework"],
        "max_level": GENERIC_BASELINE["max_level"],
        "questions": _questions(GENERIC_BASELINE),
    }
    return out


def _pick(spec, answers: dict, *, kind: str) -> tuple[list[dict], list[dict]]:
    """按 spec 校验答案，返回 (选项, 逐项明细)。缺项或未知取值都拒绝。"""
    if answers is None:
        answers = {}
    if not isinstance(answers, dict):
        raise ValueError(f"{kind}答案必须是一个对象")
    chosen, detail = [], []
    for q in spec["questions"]:
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


def baseline_level(answers: dict, skill_id: str | None = None) -> dict:
    """自评 → 起始等级。分数越高代表「越像已经练到那一步的人」。

    同一份答案在不同技能下会得到不同等级，因为题库和标准不同；
    自评不给经验，只把等级下限抬到自评档位，但不超过该技能的 max_level。
    """
    spec = _spec(skill_id)
    chosen, detail = _pick(spec, answers, kind="自评")
    points = sum(o["points"] for o in chosen)
    best = sum(max(o["points"] for o in q["options"]) for q in spec["questions"])
    ratio = (points / best) if best else 0.0

    cuts = spec.get("cuts") or _LEVEL_CUTS
    level = 1
    for cut, lv in cuts:
        if ratio >= cut and lv <= spec["max_level"]:
            level = lv
            break
    level = max(1, min(spec["max_level"], level))

    # 最高档额外要求：至少 3 题落在该题最高档的 80% 以上。
    anchors = 0
    for opt, q in zip(chosen, spec["questions"]):
        top = max(o["points"] for o in q["options"])
        if top and opt["points"] >= top * 0.8:
            anchors += 1
    capped = False
    if (spec["max_level"] >= 6 and level >= spec["max_level"]
            and anchors < _TOP_ANCHORS_FOR_MAX):
        level = spec["max_level"] - 1
        capped = True

    strong = [
        d["label"] for d, o, q in zip(detail, chosen, spec["questions"])
        if max(x["points"] for x in q["options"]) and
        o["points"] >= max(x["points"] for x in q["options"]) * 0.8
    ]

    why = {
        1: "从零开始：起点 LV1，经验和门槛都按正常路径走。",
        2: "有一点底子：给 LV2 的信用，LV3 起仍要经验与达标校验。",
        3: "已经入门：给 LV3 的信用，从 LV4 起必须靠练。",
        4: "稳定会用：给 LV4 的信用，往上要近期练习与客观成绩。",
        5: "接近专业：给 LV5 的信用；LV6 需要更多外部锚点。",
        6: "按标准已是高水平：自评最高给到 LV6，LV7–8 必须靠复测。",
    }[level]
    if capped:
        why += "（你的总分够高，但高分档不足 3 项，先给到低一级。）"

    return {
        "skill_id": skill_id or "",
        "level": level,
        "points": points,
        "max_points": best,
        "percent": round(ratio * 100, 1),
        "max_level": spec["max_level"],
        "framework": spec["framework"],
        "anchors": anchors,
        "anchors_needed": _TOP_ANCHORS_FOR_MAX,
        "capped_by_anchors": capped,
        "strong_evidence": strong[:5],
        "detail": detail,
        "why": why,
    }


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


def session_questions() -> list[dict]:
    return _questions({"questions": SESSION_QUESTIONS})


def session_factor_bps(answers: dict) -> dict:
    """这一次练习的微调系数：四项连乘后截断到 8000–11200 基点。"""
    chosen, detail = _pick({"questions": SESSION_QUESTIONS}, answers, kind="练习")
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
