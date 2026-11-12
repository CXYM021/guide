# -*- coding: utf-8 -*-
"""低摩擦采集层 · 候选时段账本（本机 JSON 存档）。

这个模块只做三件事：
  1 把「本页自己的可见 / 隐藏区间」折成一个可复现的候选时长；
  2 从真实事件日志生成预填值，并生成最多两问的结算小问卷；
  3 在用户【显式确认】之后，调用外部注入的练习函数入账。

硬约束：候选时段绝不自动折算熟练度。本模块不 import ProficiencySystem，
不写 total_exp、不推连胜、不改保持度，所有加分仍走 add_practice 的原判定门。

能力边界（经一手来源核实，见 realistic_rules.CAPABILITY_VERIFICATION）：
  网页只能观察自己这一页的 visibilityState；读不到手机屏幕亮灭的独立状态、
  读不到前台 App、页面隐藏后 JS 被限流，无法后台常驻监控。
因此这里记录的时长是「本页在前台的时长」，不是「用户在练习的时长」，
只作为待确认的候选证据。

只用 Python 标准库。时钟由外部注入，便于测试与网页的模拟时钟对齐。
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from realistic_rules import (
    FEEDBACK_LEVELS, LOW_FRICTION_RULES, MEDICAL_KEYS,
    settlement_form, skill_rule_for,
)

R = LOW_FRICTION_RULES

ACTIVE = "active"
PENDING = "pending"
SETTLED = "settled"
REJECTED = "rejected"
DISCARDED = "discarded"
EXPIRED = "expired"
STATUSES = (ACTIVE, PENDING, SETTLED, REJECTED, DISCARDED, EXPIRED)
TERMINAL = (SETTLED, REJECTED, DISCARDED, EXPIRED)

SCHEMA = 1
MAX_INTERVALS = 200
NOTE_TAG_PREFIX = "[快速结算#"


def _require_int(value: object, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} 必须是 >= {minimum} 的整数")
    return value


def normalize_hidden(duration: int, raw: object) -> list[list[int]]:
    """把前端提交的隐藏区间折成规范形式。纯函数。

    raw 是 [[相对开工的秒数, 相对开工的秒数], ...]，不是绝对时间戳：
    网页用模拟时钟，客户端墙钟与服务端不对齐，用相对偏移才能复现。
    区间必须有序、不重叠、落在 [0, duration] 内。
    """
    duration = _require_int(duration, "duration")
    if not isinstance(raw, list):
        raise ValueError("hidden_intervals 必须是数组")
    if len(raw) > MAX_INTERVALS:
        raise ValueError(f"hidden_intervals 最多 {MAX_INTERVALS} 段")
    out: list[list[int]] = []
    prev_end = 0
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("每段隐藏区间必须是 [起点秒, 终点秒]")
        start = _require_int(item[0], "隐藏区间起点")
        end = _require_int(item[1], "隐藏区间终点")
        if not (0 <= start < end <= duration) or start < prev_end:
            raise ValueError("隐藏区间越界、倒置或重叠")
        out.append([start, end])
        prev_end = end
    return out


def clamp_hidden(duration: int, raw: object) -> list[list[int]]:
    """把客户端墙钟算出的隐藏区间对齐到服务端时长。纯函数。

    网页用可推进的模拟时钟，客户端 Date.now() 与服务端 clock() 不保证一致；
    这里只做「截断到 [0, duration] + 合并重叠」，不凭空补区间、不改练习判定。
    """
    duration = _require_int(duration, "duration")
    if not isinstance(raw, list):
        raise ValueError("hidden_intervals 必须是数组")
    if len(raw) > MAX_INTERVALS:
        raise ValueError(f"hidden_intervals 最多 {MAX_INTERVALS} 段")
    pieces = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("每段隐藏区间必须是 [起点秒, 终点秒]")
        s, e = item[0], item[1]
        if isinstance(s, bool) or isinstance(e, bool) \
                or not isinstance(s, int) or not isinstance(e, int) \
                or s < 0 or e < 0:
            raise ValueError("隐藏区间端点必须是非负整数")
        s, e = min(s, duration), min(e, duration)
        if s < e:
            pieces.append([s, e])
    pieces.sort(key=lambda x: x[0])
    merged: list[list[int]] = []
    for s, e in pieces:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def fold_visible(duration: int, intervals: list[list[int]]) -> tuple[int, int]:
    """返回 (前台可见秒数, 隐藏秒数)。纯函数，全整数。"""
    hidden = sum(e - s for s, e in intervals)
    if hidden > duration:
        raise ValueError("隐藏总时长不能超过整个时段")
    return duration - hidden, hidden


def prefill_from_event(event: dict | None, skill_id: str) -> dict:
    """从该技能最近一次【有效练习事件】生成预填值。纯函数。

    只继承自报的判断参数，不继承证明声明与随记——那些是上一次的声明，
    沿用会变成伪造证据。没有可用事件时 available=False，结算必须走完整表单。
    """
    empty = {"available": False, "source_seq": None, "source_at": None,
             "values": {}, "reason": "这个技能还没有可用的有效练习记录，需先完整填一次",
             "disclaimer": R["prefill_disclaimer"]}
    if not isinstance(event, dict) or event.get("kind") != "practice" \
            or int(event.get("delta", 0) or 0) <= 0:
        return empty
    rule = skill_rule_for(skill_id)
    if rule is None:
        return {**empty, "reason": "该技能没有专属判定规则，需完整填写"}
    metrics = event.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != set(rule.required_keys):
        return {**empty, "reason": "上次记录的指标字段与现行规则不一致，需重新填写"}
    for spec in rule.metrics:
        v = metrics.get(spec.key)
        if isinstance(v, bool) or not isinstance(v, int) \
                or not (spec.range_low <= v <= spec.range_high):
            return {**empty, "reason": "上次记录的指标值不合法，需重新填写"}
    difficulty, quality = event.get("difficulty"), event.get("quality")
    feedback, task_key = event.get("feedback"), event.get("task_key")
    if (isinstance(difficulty, bool) or not isinstance(difficulty, int)
            or not (1 <= difficulty <= 5)
            or isinstance(quality, bool) or not isinstance(quality, int)
            or not (0 <= quality <= 100)
            or feedback not in FEEDBACK_LEVELS
            or not isinstance(task_key, str) or not task_key):
        return {**empty, "reason": "上次记录的自评字段不合法，需重新填写"}
    if rule.medical_gate:
        # 安全门三项不能沿用：疼痛 / RPE / 停止信号是本次的身体状态。
        return {**empty,
                "reason": "跑步与健身的安全门三项必须本次重新确认，不能沿用"}
    return {
        "available": True,
        "source_seq": int(event.get("seq", 0)),
        "source_at": int(event.get("at", 0)),
        "values": {
            "difficulty": difficulty, "quality": quality,
            "feedback": feedback, "task_key": task_key,
            "metrics": dict(metrics),
        },
        "reason": "来自上一次有效练习的自报值，不是本次测量",
        "disclaimer": R["prefill_disclaimer"],
    }


def questionnaire_for(visible_seconds: int, prefill: dict) -> list[dict]:
    """最多两问。只问会改变结果的事；纯函数。"""
    mins = max(1, round(visible_seconds / 60))
    q1 = {
        "key": "confirm",
        "label": f"这段时间（本页在前台约 {mins} 分钟）算一次有效练习吗？",
        "kind": "choice",
        "options": [
            {"key": "yes", "label": "算，记一笔"},
            {"key": "no", "label": "不算，丢掉"},
            {"key": "later", "label": "先留着，回头再说"},
        ],
        "default": "yes",
        "required": True,
        "why": R["no_auto_credit_note"],
    }
    if prefill.get("available"):
        q2 = {
            "key": "values",
            "label": "这次的观测值怎么处理？",
            "kind": "choice",
            "options": [
                {"key": "same", "label": "和上次差不多，沿用"},
                {"key": "new_task", "label": "数值沿用，但这次换了任务"},
                {"key": "edit", "label": "我自己改，展开完整表单"},
            ],
            "default": "same",
            "required": True,
            "why": prefill.get("disclaimer", ""),
        }
    else:
        q2 = {
            "key": "values",
            "label": "这次需要填一次观测值",
            "kind": "choice",
            "options": [{"key": "edit", "label": "去填写（本次唯一需要填的一步）"}],
            "default": "edit",
            "required": True,
            "why": prefill.get("reason", ""),
        }
    return [q1, q2]


def build_practice_body(skill_id: str, fields: dict, *, note: str = "",
                        visible_seconds: int = 0, tag: str = "",
                        mode_text: str = "本次问卷自报") -> dict:
    """把技能专属问卷的答案组装成 add_practice 的请求体。纯函数。

    字段严格对齐已有判定口径，不新增任何标准：
      required_keys → metrics；difficulty / quality / feedback / task_key 原样映射；
      note 写清「本次问卷自报」与候选时长；证明声明一律留空（不继承、不伪造）。
    数值范围在这里先做一次可读校验，最终判定仍由 add_practice 全权执行。
    """
    rule = skill_rule_for(skill_id)
    if rule is None:
        raise ValueError(f"技能 {skill_id!r} 没有专属判定规则")
    if not isinstance(fields, dict):
        raise ValueError("问卷答案 fields 必须是对象")

    def as_int(key: str, label: str) -> int:
        v = fields.get(key)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{label} 必须是整数，不能留空或填小数")
        return v

    task = fields.get("task_custom") or fields.get("task")
    task = str(task or "").strip()
    if not task:
        raise ValueError("请选或填这次做的任务（卷子 / 训练 / 材料名）")
    difficulty = as_int("difficulty", "难度")
    if not 1 <= difficulty <= 5:
        raise ValueError("难度必须是 1–5")
    quality = as_int("quality", "完成质量")
    if not 0 <= quality <= 100:
        raise ValueError("完成质量必须是 0–100")
    feedback = fields.get("feedback")
    if feedback not in FEEDBACK_LEVELS:
        raise ValueError("反馈取值不合法")

    metrics = {k: as_int(k, k) for k in rule.required_keys}
    for m in rule.metrics:
        v = metrics[m.key]
        if not (m.range_low <= v <= m.range_high):
            raise ValueError(f"{m.label} 必须在 {m.range_low}–{m.range_high} 之间")
    if rule.medical_gate:
        for k in MEDICAL_KEYS:
            as_int(k, k)

    head = f"{tag} {mode_text}；候选前台时长 " \
           f"{max(0, int(visible_seconds)) // 60} 分钟（时长不折算经验）"
    # 问卷里的随记优先；没有就用挂机时留下的随记。
    user_note = str(fields.get("note") or note or "").strip()
    full_note = (head + (f"；{user_note}" if user_note else ""))[:500]
    return {
        "skill_id": skill_id,
        "difficulty": difficulty, "quality": quality,
        "feedback": feedback, "task_key": task,
        "metrics": metrics, "note": full_note,
        "proof_type": "", "proof_url": "",
    }


def compose_note(tag: str, mode_text: str, visible_seconds: int,
                 note: str) -> str:
    """统一的结算随记前缀，保证事件日志可回溯、不自称仪器测量。"""
    head = f"{tag} {mode_text}；候选前台时长 " \
           f"{max(0, int(visible_seconds)) // 60} 分钟（时长不折算经验）"
    user_note = str(note or "").strip()
    return (head + (f"；{user_note}" if user_note else ""))[:500]


def resolve_settle_body(session: dict, answers: dict, t: int) -> tuple[dict, str]:
    """两种结算路径，纯函数：技能专属问卷（默认） / 沿用上次自评（一键捷径）。

    旧版按 answers.values / full_form 提交的写法继续可用，避免历史小票失效。
    两条路径最后都返回同一个 add_practice 请求体结构，判定口径完全一致。
    """
    sid = str(session["id"])
    skill_id = str(session["skill_id"])
    tag = f"{NOTE_TAG_PREFIX}{sid}]"
    visible = int(session.get("visible_seconds", 0))
    note = str(session.get("note", ""))
    mode = answers.get("mode")
    legacy = (mode == "reuse"
              or (mode is None and ("values" in answers
                                   or "full_form" in answers)))

    if legacy:
        choice = answers.get("values", "same")
        if choice == "edit":
            full = answers.get("full_form")
            if not isinstance(full, dict):
                raise ValueError("选择自己填时必须提供 full_form")
            values = {
                "difficulty": full["difficulty"], "quality": full["quality"],
                "feedback": full["feedback"], "task_key": full["task_key"],
                "metrics": full["metrics"],
            }
            mode_text = "本次重新填写"
        else:
            prefill = session.get("prefill") or {}
            if not prefill.get("available"):
                raise ValueError("没有可沿用的记录，必须按问卷填写")
            values = dict(prefill["values"])
            if choice == "new_task":
                day = datetime.datetime.fromtimestamp(
                    t, datetime.timezone.utc).strftime("%Y%m%d")
                values["task_key"] = f"{values['task_key']}·{day}·{sid[-4:]}"
                mode_text = "沿用上次自评，任务标识改为本次"
            elif choice == "same":
                mode_text = "沿用上次自评"
            else:
                raise ValueError("values 只能是 same / new_task / edit")
        return {
            "skill_id": skill_id,
            "difficulty": values["difficulty"], "quality": values["quality"],
            "feedback": values["feedback"], "task_key": values["task_key"],
            "metrics": values["metrics"],
            "note": compose_note(tag, mode_text, visible, note),
            # 证明声明不继承：上一次的声明不是这一次的证据。
            "proof_type": "", "proof_url": "",
        }, mode_text

    fields = answers.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("结算问卷必须提供 fields")
    body = build_practice_body(
        skill_id, fields, note=str(fields.get("note") or note),
        visible_seconds=visible, tag=tag, mode_text="本次问卷自报")
    return body, "本次问卷自报"


@dataclass
class CaptureSession:
    id: str
    skill_id: str
    started_at: int
    ended_at: int = 0
    status: str = ACTIVE
    duration_seconds: int = 0
    visible_seconds: int = 0
    hidden_seconds: int = 0
    hidden_intervals: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    note: str = ""
    prefill: dict = field(default_factory=dict)
    questions: list = field(default_factory=list)
    form: dict = field(default_factory=dict)
    answers: dict = field(default_factory=dict)
    event_seq: int | None = None
    reason: str = ""
    settled_at: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "skill_id": self.skill_id,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "status": self.status, "duration_seconds": self.duration_seconds,
            "visible_seconds": self.visible_seconds,
            "hidden_seconds": self.hidden_seconds,
            "hidden_intervals": [list(x) for x in self.hidden_intervals],
            "signals": dict(self.signals), "note": self.note,
            "prefill": dict(self.prefill), "questions": list(self.questions),
            "form": dict(self.form),
            "answers": dict(self.answers), "event_seq": self.event_seq,
            "reason": self.reason, "settled_at": self.settled_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "CaptureSession":
        if not isinstance(d, dict):
            raise ValueError("候选时段必须是对象")
        return CaptureSession(
            id=str(d["id"]), skill_id=str(d["skill_id"]),
            started_at=_require_int(d["started_at"], "started_at"),
            ended_at=_require_int(d.get("ended_at", 0), "ended_at"),
            status=str(d.get("status", ACTIVE)),
            duration_seconds=_require_int(d.get("duration_seconds", 0), "duration_seconds"),
            visible_seconds=_require_int(d.get("visible_seconds", 0), "visible_seconds"),
            hidden_seconds=_require_int(d.get("hidden_seconds", 0), "hidden_seconds"),
            hidden_intervals=[list(x) for x in (d.get("hidden_intervals") or [])],
            signals=dict(d.get("signals") or {}), note=str(d.get("note", "")),
            prefill=dict(d.get("prefill") or {}),
            questions=list(d.get("questions") or []),
            form=dict(d.get("form") or {}),
            answers=dict(d.get("answers") or {}),
            event_seq=d.get("event_seq"), reason=str(d.get("reason", "")),
            settled_at=_require_int(d.get("settled_at", 0), "settled_at"),
        )


class CaptureLedger:
    """候选时段账本。所有写操作串行化，落盘用临时文件原子替换。"""

    def __init__(self, path: str | os.PathLike[str], clock=None,
                 rules: dict | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: int(datetime.datetime.now().timestamp()))
        self.rules = dict(rules or R)
        self._lock = threading.RLock()
        self._sessions: list[CaptureSession] = []
        self._load()

    # ---------- 存档 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if int(raw.get("version", 0)) != SCHEMA:
            raise ValueError(f"候选时段存档版本不匹配: {raw.get('version')}")
        rows = raw.get("sessions") or []
        if not isinstance(rows, list):
            raise ValueError("sessions 必须是数组")
        self._sessions = [CaptureSession.from_dict(x) for x in rows]
        ids = [s.id for s in self._sessions]
        if len(ids) != len(set(ids)):
            raise ValueError("候选时段 id 重复")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": SCHEMA,
                   "sessions": [s.to_dict() for s in self._sessions]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def _find(self, session_id: str) -> CaptureSession:
        for s in self._sessions:
            if s.id == session_id:
                return s
        raise KeyError(f"找不到候选时段 {session_id!r}")

    def get(self, session_id: str) -> dict:
        """只读查询单张小票。"""
        with self._lock:
            return self._find(str(session_id)).to_dict()

    def _next_id(self, now: int) -> str:
        n = 1 + max((int(s.id.rsplit("-", 1)[-1]) for s in self._sessions
                     if s.id.startswith("c") and s.id.rsplit("-", 1)[-1].isdigit()),
                    default=0)
        return f"c{now:010d}-{n:04d}"

    # ---------- 启动自愈 ----------
    def boot_reconcile(self, now: int | None = None) -> int:
        """启动时收口：不自动加分，只把超时挂机/过期小票标为作废。"""
        t = int(now if now is not None else self.clock())
        changed = 0
        cap = int(self.rules["max_session_seconds"])
        ttl = int(self.rules["pending_ttl_days"]) * 86_400
        with self._lock:
            for s in self._sessions:
                if s.status == ACTIVE and t - s.started_at > cap:
                    # 超时挂机直接作废：不推测这段时间里哪一段在前台，
                    # 可见时长记 0，不凭空补一段候选证据。
                    s.status, s.reason = EXPIRED, "BOOT_OVER_MAX_SESSION"
                    s.ended_at = s.started_at + cap
                    s.duration_seconds = 0
                    s.visible_seconds = 0
                    s.hidden_seconds = 0
                    s.hidden_intervals = []
                    changed += 1
                elif s.status == PENDING and s.ended_at \
                        and t - s.ended_at > ttl:
                    s.status, s.reason = EXPIRED, "PENDING_TTL_EXPIRED"
                    changed += 1
            if changed:
                self._save()
        return changed

    # ---------- 写操作 ----------
    def start(self, skill_id: str, *, now: int | None = None) -> dict:
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise ValueError("skill_id 不能为空")
        t = int(now if now is not None else self.clock())
        with self._lock:
            self.boot_reconcile(t)
            for s in self._sessions:
                if s.status == ACTIVE and s.skill_id == skill_id:
                    raise ValueError("这个技能已经有一段挂机未收口")
            if sum(1 for s in self._sessions if s.status == ACTIVE) \
                    >= int(self.rules["max_active"]):
                raise ValueError("同时只允许一段挂机，请先收口")
            session = CaptureSession(
                id=self._next_id(t), skill_id=skill_id, started_at=t,
                signals={"manual_start": 1, "source": self.rules["signal_source"]},
            )
            self._sessions.append(session)
            self._save()
            return session.to_dict()

    def stop(self, session_id: str, hidden_intervals: object, *,
             last_event: dict | None = None, note: str = "",
             level: int = 1, now: int | None = None,
             form_rule=None, form_spec: dict | None = None) -> dict:
        t = int(now if now is not None else self.clock())
        with self._lock:
            s = self._find(str(session_id))
            if s.status != ACTIVE:
                raise ValueError(f"该时段状态为 {s.status}，不能收口")
            duration = t - s.started_at
            if duration < 0:
                raise ValueError("收口时间早于开工时间")
            intervals = normalize_hidden(
                duration, clamp_hidden(duration, hidden_intervals))
            visible, hidden = fold_visible(duration, intervals)
            s.ended_at = t
            s.duration_seconds = duration
            s.hidden_intervals = intervals
            s.visible_seconds = visible
            s.hidden_seconds = hidden
            s.note = str(note)[:200]
            s.signals = {"manual_start": 1, "page_hidden": len(intervals),
                         "page_visible": len(intervals), "manual_stop": 1,
                         "source": self.rules["signal_source"]}
            prefill = prefill_from_event(last_event, s.skill_id)
            s.prefill = prefill
            # 收工后按技能生成结算问卷：问题、默认选中值、选项全部来自规则表。
            form = settlement_form(s.skill_id, level=max(1, int(level)),
                                   prefill=prefill, rule=form_rule,
                                   form_spec=form_spec)
            s.form = form
            s.questions = form["questions"]
            if duration < int(self.rules["min_session_seconds"]):
                s.status, s.reason = DISCARDED, "TOO_SHORT"
            elif duration > int(self.rules["max_session_seconds"]):
                s.status, s.reason = EXPIRED, "OVER_MAX_SESSION"
            elif visible < int(self.rules["min_session_seconds"]):
                s.status, s.reason = DISCARDED, "TOO_LITTLE_VISIBLE"
            elif sum(1 for x in self._sessions if x.status == PENDING) \
                    >= int(self.rules["max_pending"]):
                s.status, s.reason = DISCARDED, "TOO_MANY_PENDING"
            else:
                s.status, s.reason = PENDING, ""
            self._save()
            return s.to_dict()

    def settle(self, session_id: str, answers: dict, practice_fn, *,
               now: int | None = None) -> tuple[dict, dict]:
        """用户确认后才入账。practice_fn(body) -> {"payload":..., "event_seq":..., "valid":..., "reason_code":...}。

        本方法不自己改经验；判定由外部的真实系统执行，结果原样回填。
        """
        if not isinstance(answers, dict):
            raise ValueError("answers 必须是对象")
        t = int(now if now is not None else self.clock())
        with self._lock:
            s = self._find(str(session_id))
            if s.status != PENDING:
                raise ValueError(f"该时段状态为 {s.status}，不能结算")
            confirm = answers.get("confirm", "yes")
            if confirm == "later":
                return s.to_dict(), {}
            if confirm == "no":
                s.status, s.reason = DISCARDED, "USER_DECLINED"
                s.answers = dict(answers)
                self._save()
                return s.to_dict(), {}
            if confirm != "yes":
                raise ValueError("confirm 只能是 yes / no / later")
            body, _mode_text = resolve_settle_body(s.to_dict(), answers, t)
            result = practice_fn(body)
            s.answers = dict(answers)
            s.event_seq = result.get("event_seq")
            s.settled_at = t
            s.status = SETTLED if result.get("valid") else REJECTED
            s.reason = "" if result.get("valid") else str(result.get("reason_code", ""))
            self._save()
            return s.to_dict(), result.get("payload") or {}

    def discard(self, session_id: str) -> dict:
        with self._lock:
            s = self._find(str(session_id))
            if s.status != PENDING:
                raise ValueError(f"该时段状态为 {s.status}，不能丢弃")
            s.status, s.reason = DISCARDED, "USER_DISCARDED"
            self._save()
            return s.to_dict()

    def clear_all(self) -> int:
        """重置进度时清空候选小票。不碰只增事件日志。"""
        with self._lock:
            n = len(self._sessions)
            self._sessions = []
            self._save()
            return n

    # ---------- 读与自检 ----------
    def verify(self, event_seqs: set[int] | None = None,
               tagged_ids: set[str] | None = None) -> list[str]:
        problems: list[str] = []
        ids = [s.id for s in self._sessions]
        if len(ids) != len(set(ids)):
            problems.append("候选时段 id 重复")
        for s in self._sessions:
            if s.status not in STATUSES:
                problems.append(f"{s.id}: 未知状态 {s.status}")
                continue
            if s.status == ACTIVE and s.ended_at != 0:
                problems.append(f"{s.id}: 进行中却已有结束时间")
            if s.status in TERMINAL and not s.ended_at:
                problems.append(f"{s.id}: 已收口却缺少结束时间")
            if s.status != ACTIVE:
                try:
                    intervals = normalize_hidden(s.duration_seconds,
                                                 s.hidden_intervals)
                    v, h = fold_visible(s.duration_seconds, intervals)
                    if (v, h) != (s.visible_seconds, s.hidden_seconds):
                        problems.append(f"{s.id}: 可见/隐藏时长与区间不自洽")
                except ValueError as exc:
                    problems.append(f"{s.id}: {exc}")
            if s.status == SETTLED:
                if not isinstance(s.event_seq, int):
                    problems.append(f"{s.id}: 已入账却缺少事件号")
                elif event_seqs is not None and s.event_seq not in event_seqs:
                    problems.append(f"{s.id}: 事件号 {s.event_seq} 不在真实日志中")
                if tagged_ids is not None and s.id not in tagged_ids:
                    problems.append(f"{s.id}: 事件日志里找不到该次结算标记")
            if s.status == REJECTED and isinstance(s.event_seq, int):
                # 技能门不通过时核心引擎零副作用、不写事件，event_seq 允许为 None。
                if event_seqs is not None and s.event_seq not in event_seqs:
                    problems.append(f"{s.id}: 事件号 {s.event_seq} 不在真实日志中")
                if tagged_ids is not None and s.id not in tagged_ids:
                    problems.append(f"{s.id}: 事件日志里找不到该次结算标记")
            cap = int(self.rules["max_questions"])
            if len(s.questions) > cap:
                problems.append(f"{s.id}: 结算问题超过上限")
            if s.form:
                qs = s.form.get("questions") or []
                rule = skill_rule_for(s.skill_id)
                needed = (set(rule.required_keys) if rule else set()) | {
                    "task", "difficulty", "feedback", "quality", "note"}
                missing = needed - {q.get("key") for q in qs}
                if missing:
                    problems.append(
                        f"{s.id}: 结算问卷缺少字段 {sorted(missing)}")
                if int(s.form.get("question_count", 0)) != len(qs) or len(qs) > cap:
                    problems.append(f"{s.id}: 结算问卷题数不自洽或超上限")
        return problems

    def snapshot(self, *, now: int | None = None,
                 event_seqs: set[int] | None = None,
                 tagged_ids: set[str] | None = None) -> dict:
        t = int(now if now is not None else self.clock())
        with self._lock:
            self.boot_reconcile(t)
            rows = [s.to_dict() for s in self._sessions]
            return {
                "ok": True,
                "sessions": rows,
                "active": [x for x in rows if x["status"] == ACTIVE],
                "pending": [x for x in rows if x["status"] == PENDING],
                "counts": {k: sum(1 for x in rows if x["status"] == k)
                           for k in STATUSES},
                "rules": dict(self.rules),
                "verify": self.verify(event_seqs, tagged_ids),
                "save_path": str(self.path),
                "clock_unix": t,
            }
