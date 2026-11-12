# -*- coding: utf-8 -*-
"""熟练度系统 · 本地网页版（现实生活技能成长规律）。

启动: python web_app.py            (自动打开浏览器)
      python web_app.py --no-browser
      python web_app.py 9000       (指定端口)

只用 Python 标准库。业务逻辑复用 proficiency.py + realistic_rules.py，
本文件只负责 HTTP 接口、模拟时钟与页面展示。
存档落在同目录 web_save/skills.json，服务重启后进度不丢。
"""

from __future__ import annotations

import contextvars
import datetime
import json
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from proficiency import (
    SCHEMA_VERSION, JsonFileStorage, PowerRule, ProficiencySystem, SkillConfig,
    register_custom_rule,
)
from realistic_rules import (
    ALTRUISM_REASON_CODES, APPLIED_DECISIONS, CAPABILITY_VERIFICATION,
    LEVEL_CRITERIA, LOW_FRICTION_RULES, MAX_LEVEL,
    NARRATIVE_RULES, OPEN_DECISIONS, REASON_CODES, SHAPE_RULES, SKILL_IDS,
    SKILL_RULES, collect_open_items, day_index, evaluate_achievements,
    realm_title, rulebook, settlement_form,
)
from benchmark import BenchmarkLedger
from capture_ledger import CaptureLedger, NOTE_TAG_PREFIX
from data_vault import (
    daily_backup, export_bundle, make_backup, validate_bundle, write_bundle,
)
from web_ui import HTML_PAGE
from world_state import WorldState
from surveys import (
    BASELINE_MAX_LEVEL, baseline_level, baseline_questions,
    session_factor_bps, session_questions,
)

# 产品层模块在上一级目录；只有产品副本会用到自定义技能，开发版不加载。
PRODUCT_DIR = Path(__file__).resolve().parent.parent
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))
from custom_skills import (  # noqa: E402
    MAX_CUSTOM_SKILLS, CustomSkillError, CustomSkillRegistry,
    settlement_spec_for,
)
from skill_forge import (  # noqa: E402
    ForgeError, VERIFIED_REFERENCES, ai_status, forge_skill_spec,
)

# 迁移兼容：历史存档可能含命名自定义规则（存档只存规则名）。
# 先注册同名实现才能加载；加载后 init_system() 会清理非 SKILL_IDS 的历史技能。
# 这行是存档迁移代码，不是技能选项。
register_custom_rule("alchemy_curve", lambda lv: 80 + 20 * lv * lv)

ROOT = Path(__file__).resolve().parent
DEFAULT_TZ_OFFSET = 8 * 3600   # 默认 UTC+08:00；0 可退回历史 UTC 口径
DEFAULT_PORT = 8765
DAY_SECONDS = 86_400

# 7 个正式技能，与 realistic_rules.SKILL_RULES 一一对应；等级统一 LV1–LV8
DEFAULT_SKILLS: dict[str, SkillConfig] = {
    "英语口语": SkillConfig(PowerRule(100, "1.5"), MAX_LEVEL),
    "英语读写": SkillConfig(PowerRule(100, "1.4"), MAX_LEVEL),
    "西班牙语": SkillConfig(PowerRule(100, "1.5"), MAX_LEVEL),
    "电脑办公软件技术": SkillConfig(PowerRule(90, "1.35"), MAX_LEVEL),
    "剪辑技术": SkillConfig(PowerRule(110, "1.55"), MAX_LEVEL),
    "跑步技术": SkillConfig(PowerRule(120, "1.6"), MAX_LEVEL),
    "健身技术": SkillConfig(PowerRule(110, "1.55"), MAX_LEVEL),
}
assert set(DEFAULT_SKILLS) == set(SKILL_IDS), "技能配置必须与专属规则表完全一致"
DEFAULT_RULE = PowerRule(100, "1.5")


class SimClock:
    """可推进的模拟时钟：默认跟真实时钟走，快进只加偏移，仍返回 Unix 秒。"""

    def __init__(self) -> None:
        self.offset = 0

    def __call__(self) -> int:
        return int(time.time()) + self.offset

    def advance_days(self, days: int) -> None:
        self.offset += days * DAY_SECONDS

    def reset(self) -> None:
        self.offset = 0


def load_settings() -> dict:
    """本机设置：日历时区 + 是否开放开发者工具（默认关闭）。"""
    out = {"tz_offset_seconds": DEFAULT_TZ_OFFSET, "dev_tools": False}
    path = STATE["paths"]["settings"]
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            value = int(raw.get("tz_offset_seconds", DEFAULT_TZ_OFFSET))
            from realistic_rules import validate_tz_offset
            out["tz_offset_seconds"] = validate_tz_offset(value)
            out["dev_tools"] = bool(raw.get("dev_tools", False))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"[web] 设置读取失败，按默认时区处理: {exc}\n")
    return out


def save_settings(data: dict) -> None:
    path = STATE["paths"]["settings"]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# 租户上下文：所有可变状态都放在「当前租户」里，在请求入口用 contextvars 绑定。
# 同一进程可同时服务多个用户，系统实例、模拟时钟、事件缓存、锁彼此隔离。
# 未绑定时回落到默认租户，保证 `python web_app.py` 单机模式行为不变。
# --------------------------------------------------------------------------
_TENANT_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "proficiency_tenant")


class _CtxProxy:
    """把模块级全局名转发到当前租户的对应字段。"""

    __slots__ = ("_key",)

    def __init__(self, key: str | None) -> None:
        self._key = key

    def _obj(self):
        tenant = current_tenant()
        return tenant if self._key is None else tenant[self._key]

    def __getattr__(self, name):
        return getattr(self._obj(), name)

    def __getitem__(self, key):
        return self._obj()[key]

    def __setitem__(self, key, value):
        self._obj()[key] = value

    def __contains__(self, key):
        return key in self._obj()

    def __iter__(self):
        return iter(self._obj())

    def __len__(self):
        return len(self._obj())

    def __bool__(self):
        return True

    def __enter__(self):
        return self._obj().__enter__()

    def __exit__(self, *exc):
        return self._obj().__exit__(*exc)

    def __repr__(self):
        return repr(self._obj())


def make_tenant(data_dir, *, tz_offset_seconds: int = DEFAULT_TZ_OFFSET,
                clock=None) -> dict:
    """为一个用户创建独立的状态容器与数据目录（文件级隔离）。"""
    from realistic_rules import validate_tz_offset
    base = Path(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    paths = {
        "data_dir": base,
        "save": base / "skills.json",
        "world": base / "world.json",
        "capture": base / "captures.json",
        "benchmark": base / "benchmarks.json",
        "decisions": base / "decisions.json",
        "settings": base / "settings.json",
        "custom_skills": base / "custom_skills.json",
    }
    return {
        "paths": paths,
        "clock": clock or SimClock(),
        "settings": {"tz_offset_seconds": validate_tz_offset(tz_offset_seconds)},
        "decisions": {},
        "recent_events": deque(maxlen=10),
        "recent_verdicts": deque(maxlen=10),
        "lock": threading.RLock(),
        "system": None, "world": None, "capture": None, "benchmark": None,
        "custom": None,
    }


def current_tenant() -> dict:
    tenant = _TENANT_CTX.get(None)
    if tenant is None:
        tenant = make_tenant(ROOT / "web_save")
        _TENANT_CTX.set(tenant)
    return tenant


def bind_tenant(tenant: dict):
    """绑定当前请求/线程的租户；返回 token 给 reset_tenant 还原。"""
    return _TENANT_CTX.set(tenant)


def reset_tenant(token) -> None:
    _TENANT_CTX.reset(token)


def bootstrap_tenant(tenant: dict | None = None) -> dict:
    """初始化（或重启后恢复）一个租户的全部状态。"""
    t = tenant or current_tenant()
    token = bind_tenant(t)
    try:
        load_decisions()
        # 备份必须在任何存档迁移 / 写入之前，否则备份到的是已改写状态。
        daily_backup(t["paths"]["data_dir"], now=t["clock"]())
        t["custom"] = CustomSkillRegistry(
            t["paths"]["custom_skills"], clock=t["clock"])
        t["system"] = init_system()
        t["world"] = WorldState(
            t["paths"]["world"], created_at=t["clock"](),
            tz_offset_seconds=t["settings"]["tz_offset_seconds"])
        t["capture"] = CaptureLedger(t["paths"]["capture"], clock=t["clock"])
        t["capture"].boot_reconcile()
        t["benchmark"] = BenchmarkLedger(t["paths"]["benchmark"],
                                         clock=t["clock"])
    finally:
        reset_tenant(token)
    return t


STATE = _CtxProxy(None)
SETTINGS = _CtxProxy("settings")
DECISION_SELECTIONS = _CtxProxy("decisions")
RECENT_EVENTS = _CtxProxy("recent_events")
RECENT_VERDICTS = _CtxProxy("recent_verdicts")
API_LOCK = _CtxProxy("lock")

# --------------------------------------------------------------------------
# 已拍板决策的结果：独立存档，与熟练度存档分开，服务重启不丢。
# --------------------------------------------------------------------------
DECISION_OPTIONS: dict[str, dict[str, dict]] = {
    d["id"]: {o["key"]: o for o in d["options"]} for d in OPEN_DECISIONS
}
DECISION_SELECTIONS: dict[str, dict] = {}


def load_decisions() -> None:
    """读取拍板结果；忽略失效的 id/key，不让脏存档污染界面。"""
    DECISION_SELECTIONS.clear()
    if not STATE["paths"]["decisions"].exists():
        return
    try:
        raw = json.loads(STATE["paths"]["decisions"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"[web] 拍板存档读取失败，按空处理: {exc}\n")
        return
    stamps = raw.get("timestamps") or {}
    for did, key in (raw.get("selections") or {}).items():
        if did in DECISION_OPTIONS and isinstance(key, str) and key in DECISION_OPTIONS[did]:
            DECISION_SELECTIONS[did] = {
                "key": key, "label": DECISION_OPTIONS[did][key]["label"],
                "at": stamps.get(did, ""),
            }


def save_decisions() -> None:
    STATE["paths"]["decisions"].parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "selections": {k: v["key"] for k, v in DECISION_SELECTIONS.items()},
        "timestamps": {k: v["at"] for k, v in DECISION_SELECTIONS.items()},
    }
    tmp = STATE["paths"]["decisions"].with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE["paths"]["decisions"])


def _record_decision(did: str, key: str) -> None:
    if not isinstance(did, str) or did not in DECISION_OPTIONS:
        raise KeyError(f"未知决策项 {did!r}")
    if not isinstance(key, str) or key not in DECISION_OPTIONS[did]:
        raise ValueError(f"决策 {did!r} 不接受选项 {key!r}")
    DECISION_SELECTIONS[did] = {
        "key": key, "label": DECISION_OPTIONS[did][key]["label"],
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }


def decisions_payload() -> dict:
    auto = collect_open_items()
    return {
        "ok": True,
        "status": "applied",
        "decisions": [dict(d) for d in OPEN_DECISIONS],
        "selections": dict(DECISION_SELECTIONS),
        "decided_count": len(DECISION_SELECTIONS),
        "total": len(OPEN_DECISIONS),
        "evidence_counts": {k: len(v) for k, v in auto.items()},
        "save_path": str(STATE["paths"]["decisions"]),
        "note": ("12 项已于 2026-11-06 全部拍板并落实到规则代码，见每项「实施结果」。"
                 "选择写入 web_save/decisions.json，重启不丢。逐项点选只更新记录，"
                 "不会自动回退或改写已实施的规则；界面会提示不一致，"
                 "点「恢复已拍板选择」可回到与规则一致的状态。"),
    }



def _iso(unix_seconds: int) -> str:
    return datetime.datetime.fromtimestamp(
        unix_seconds, datetime.timezone.utc
    ).isoformat(timespec="seconds")


def init_system() -> ProficiencySystem:
    registry = STATE.get("custom")
    custom_ids = set(registry.ids()) if registry else set()
    system = ProficiencySystem(
        JsonFileStorage(STATE["paths"]["save"]), clock=STATE["clock"],
        tz_offset_seconds=SETTINGS["tz_offset_seconds"],
        extra_rules=registry.rules() if registry else None)
    # 只清理既不属于内置技能、也没登记在本用户自定义账本里的历史技能。
    # 规则表按租户注入，不写模块级 SKILL_RULES，多用户之间不串。
    for sid in list(system.list_progress(now=STATE["clock"]())):
        if sid not in SKILL_IDS and sid not in custom_ids:
            system.remove_skill(sid)
    for name, cfg in DEFAULT_SKILLS.items():
        system.register_skill(name)  # 幂等，不覆盖已有存档里的配置
    for sid in sorted(custom_ids):
        system.register_skill(sid)
    system.on_level_up(lambda e: RECENT_EVENTS.appendleft({
        "kind": "up", "skill_id": e.skill_id,
        "from_level": e.from_level, "to_level": e.to_level, "at": e.at,
    }))
    system.on_level_down(lambda e: RECENT_EVENTS.appendleft({
        "kind": "down", "skill_id": e.skill_id,
        "from_level": e.from_level, "to_level": e.to_level, "at": e.at,
    }))
    system.flush()
    return system


def reload_state() -> None:
    """导入备份后重建内存对象；只增事件日志与存档文件保持一致。"""
    SETTINGS.clear()
    SETTINGS.update(load_settings())
    STATE["custom"] = CustomSkillRegistry(
        STATE["paths"]["custom_skills"], clock=STATE["clock"])
    STATE["system"] = init_system()
    STATE["world"] = WorldState(STATE["paths"]["world"], created_at=STATE["clock"](),
                                tz_offset_seconds=SETTINGS["tz_offset_seconds"])
    STATE["capture"] = CaptureLedger(STATE["paths"]["capture"], clock=STATE["clock"])
    STATE["capture"].boot_reconcile()
    STATE["benchmark"] = BenchmarkLedger(STATE["paths"]["benchmark"], clock=STATE["clock"])
    RECENT_EVENTS.clear()
    RECENT_VERDICTS.clear()


def level_thresholds() -> list[dict]:
    """默认幂律曲线下，达到每一级所需的有效经验与逐级成本。"""
    rows, cumulative = [], 0
    for level, c in enumerate(LEVEL_CRITERIA, start=1):
        cost = DEFAULT_RULE.cost(level) if level < len(LEVEL_CRITERIA) else 0
        rows.append({
            **dict(c),
            "min_effective_exp": cumulative,
            "cost_to_next": cost,
        })
        cumulative += cost
    return rows


def effective_rule(skill_id: str):
    """内置技能 + 当前用户自定义技能；不修改全局 SKILL_RULES。"""
    registry = STATE.get("custom")
    if registry:
        rule = registry.rules().get(skill_id)
        if rule is not None:
            return rule
    return SKILL_RULES.get(skill_id)


def custom_form_parts(skill_id: str):
    registry = STATE.get("custom")
    return registry.form_for(skill_id) if registry else None


def custom_skills_payload() -> dict:
    registry = STATE.get("custom")
    return {
        "ok": True,
        "skills": registry.public_list() if registry else [],
        "verify": registry.verify() if registry else [],
        "max_custom_skills": MAX_CUSTOM_SKILLS,
        "ai": ai_status(),
        "verified_references": VERIFIED_REFERENCES,
        "builtin_skill_ids": list(SKILL_IDS),
        "note": ("AI 需要部署方配置 OpenAI 兼容端点；未配置时使用本地保守模板，"
                 "不会冒充 AI 联网判定。生成的规则落盘后按原纯函数判定。"),
    }


def add_custom_skill(body: dict) -> dict:
    spec = forge_skill_spec(
        name=body.get("name"), metrics=body.get("metrics"),
        source_urls=body.get("source_urls"), now=STATE["clock"]())
    registry: CustomSkillRegistry = STATE["custom"]
    registry.add(spec)
    system: ProficiencySystem = STATE["system"]
    system.set_extra_rules(registry.rules())
    system.register_skill(spec["skill_id"])
    system.flush()
    return {"ok": True, "provenance": spec["provenance"],
            **custom_skills_payload()}


def remove_custom_skill(body: dict) -> dict:
    sid = str(body.get("skill_id") or "").strip()
    registry: CustomSkillRegistry = STATE["custom"]
    registry.remove(sid)          # 内置技能与不存在的名字在这里被拒
    capture = STATE["capture"]
    for row in capture.snapshot().get("sessions", []):
        if row.get("skill_id") == sid and row.get("status") == "pending":
            try:
                capture.discard(str(row["id"]))   # 待结算小票不能再入账
            except (ValueError, KeyError):
                pass
    system: ProficiencySystem = STATE["system"]
    system.remove_skill(sid)      # 事件日志按只增保留，历史仍可回溯
    system.set_extra_rules(registry.rules())
    system.flush()
    return {"ok": True, "removed": sid, **custom_skills_payload()}


def rules_payload() -> dict:
    out = {
        "ok": True,
        **rulebook(),
        "level_thresholds": level_thresholds(),
        "reason_order": [
            "INVALID_INPUT", "TIME_REVERSED", "QUALITY_BELOW_MIN",
            "NO_FEEDBACK_LOW_QUALITY", "LOW_DIFFICULTY_STREAK",
            "REPEATED_TASK", "OK",
        ],
        "schema_version": SCHEMA_VERSION,
    }
    registry = STATE.get("custom")
    if registry:
        merged_rules = dict(out.get("skill_rules") or {})
        merged_forms = dict(out.get("settlement_forms") or {})
        specs = registry.specs()
        for sid, rule in registry.rules().items():
            merged_rules[sid] = rule.to_dict()
            merged_forms[sid] = settlement_form(
                sid, rule=rule,
                form_spec=settlement_spec_for(
                    {**specs[sid], "skill_id": sid}))
        out["skill_rules"] = merged_rules
        out["settlement_forms"] = merged_forms
    return out


def event_seq_sets() -> tuple[set[int], set[int]]:
    """利他行为必须锚定的真实事件集合，全部来自 append-only 事件日志。"""
    system: ProficiencySystem = STATE["system"]
    all_seqs = {int(e["seq"]) for e in system.events()}
    practice_seqs = {int(e["seq"]) for e in system.events(kinds=("practice",))}
    return all_seqs, practice_seqs


def world_payload() -> dict:
    world: WorldState = STATE["world"]
    all_seqs, practice_seqs = event_seq_sets()
    return {
        "ok": True,
        **world.snapshot(now=STATE["clock"](), event_seqs=all_seqs,
                         practice_seqs=practice_seqs),
        "save_path": str(STATE["paths"]["world"]),
    }


def narrative_for(result, *, buff_used: bool) -> str:
    """网文式即时反馈。纯文案，不引用任何未经核实的留存数字。"""
    v = result.verdict
    if not v.valid:
        return REASON_CODES.get(v.reason_code, "这次没有计入，看看哪里没达标")
    remaining = result.after.remaining_exp
    if remaining <= 0:
        text = f"叮！宿主熟练度 +{v.gain}，本级已圆满，待境界校验放行。"
    else:
        text = NARRATIVE_RULES["feedback_template"].format(
            gain=v.gain, remaining=remaining
        )
    if buff_used:
        text += " · 悟道卡生效（本次翻倍，已消耗）"
    if result.levels_gained > 0:
        text += " · " + NARRATIVE_RULES["breakthrough_template"].format(
            realm=realm_title(result.after.level)
        )
    return text


def _missing_items(info: dict) -> list[str]:
    """卡级时把「还差哪几项」翻译成可直接执行的短句，一项不漏。"""
    nc = info.get("next_level_criteria") or {}
    rows = []
    for c in nc.get("checks", []):
        if c.get("passed"):
            continue
        action = str(c.get("action") or "").strip()
        rows.append(action or
                    f"{c['label']}（现 {c['actual']} / 需 {c['required']}）")
    return rows


def detect_stagnation(events: list[dict], rule, *, window: int = 6) -> dict | None:
    """最近 N 次有效练习的主指标没有增长时给「该换任务」提示。纯函数。

    只看真实事件日志里的自报观测值；比较前 3 次与后 3 次均值，
    允许 2% 的测量抖动。不把它当作判定规则，只是界面引导。
    """
    if window < 6 or rule is None or not rule.metrics:
        return None
    spec = rule.metrics[0]
    rows = [e for e in events
            if e.get("kind") == "practice" and e.get("skill_id") == rule.skill_id]
    rows = sorted(rows, key=lambda e: int(e.get("at", 0)))[-window:]
    if len(rows) < window:
        return None
    values = [int((e.get("metrics") or {}).get(spec.key, 0)) for e in rows]
    head = sum(values[:3]) / 3
    tail = sum(values[-3:]) / 3
    if head <= 0 or tail > head * 1.02:
        return None
    return {
        "window": window, "metric_key": spec.key, "metric_label": spec.label,
        "unit": spec.unit, "first_half_avg": round(head, 2),
        "last_half_avg": round(tail, 2), "values": values,
        "hint": (f"最近 {window} 次「{spec.label}」停在 "
                 f"{round(tail, 1)} {spec.unit} 没涨，"
                 "继续按同一难度练大概率还是原地踏步；换任务或提高难度"),
    }


def _primary_gap(info: dict) -> tuple[float, dict | None]:
    """从下一级未通过项里挑相对缺口最大的一项；技能专属指标优先。"""
    nc = info.get("next_level_criteria") or {}
    best, best_score = None, -1.0
    for c in nc.get("checks", []):
        if c.get("passed"):
            continue
        req, act = c.get("required"), c.get("actual")
        if isinstance(req, (int, float)) and isinstance(act, (int, float)) \
                and not isinstance(req, bool) and not isinstance(act, bool) \
                and req > 0:
            rel = min(1.0, max(0.0, (req - act) / max(1.0, req)))
        else:
            rel = 0.5
        score = rel + (0.15 if c.get("is_skill_metric") else 0.0)
        if score > best_score:
            best, best_score = c, score
    return max(0.0, best_score), best


def guidance(system: ProficiencySystem, now: int) -> dict:
    """今日最优一手 + 各技能卡级缺项 + 停滞提示。全部由已有状态推导。

    评分（只用于界面排序，不参与任何判定）：
      0.35×离升级最近 + 0.25×保持度掉得快 + 0.15×最久没练 + 0.25×最大短板缺口
    动作不再固定取 metrics[0]，而是取「相对缺口最大」的未通过项；
    技能专属指标额外加权，避免「短板是听者可理解度却一直被推去拉时长」。
    """
    events = system.events(kinds=("practice",))
    gains: dict[str, int] = {}
    for e in events:   # 事件日志按时间倒序
        gains.setdefault(e["skill_id"], max(0, int(e["delta"])))
    scored, missing_by_skill = [], {}
    for sid, p in system.list_progress(now=now).items():
        info = system.assess(sid, now=now)
        missing_by_skill[sid] = _missing_items(info)
        rule = effective_rule(sid)
        if rule is None:
            continue
        closeness = 1.0 - min(1.0, max(0, p.remaining_exp) / max(1, p.required_exp))
        rust = 1.0 - p.retention_bps / 10_000
        idle = min(1.0, p.idle_days_bps / (30 * 10_000))
        gap, primary = _primary_gap(info)
        score = 0.35 * closeness + 0.25 * rust + 0.15 * idle + 0.25 * gap
        scored.append((score, sid, p, rule, info, gap, primary))
    if not scored:
        return {"recommendation": None, "missing": missing_by_skill}
    score, sid, p, rule, info, gap, primary = max(scored, key=lambda x: x[0])
    target = min(MAX_LEVEL, p.level + 1)
    action = (str(primary.get("action") or "").strip() if primary else "")
    if not action:
        spec = rule.metrics[0]
        action = f"{spec.label}达到 {spec.level_requirements[target - 1]} {spec.unit}"
    return {
        "recommendation": {
            "skill_id": sid, "score": round(score, 3),
            "realm": realm_title(p.level),
            "action": action,
            "primary_gap_key": primary.get("key") if primary else None,
            "primary_gap_score": round(gap, 3),
            "target_level": target,
            "estimated_gain": gains.get(sid) or (20 * max(1, p.level) * 80 // 100),
            "estimate_note": "按该技能最近一次有效练习的增益估算",
            "remaining_exp": max(0, p.remaining_exp),
            # 全部缺项都给出去，不再只截前两条造成「只说一半」。
            "missing": missing_by_skill.get(sid, []),
            "stagnation": detect_stagnation(events, rule),
            "score_formula": "0.35×升级接近度 + 0.25×生疏 + 0.15×搁置 + 0.25×最大短板缺口",
            "window_used": bool(info["criteria"].get("window_used")),
        },
        "missing": missing_by_skill,
    }


def task_timeline(system: ProficiencySystem, limit_groups: int = 8) -> list[dict]:
    """同一任务纵向对比 + 个人最好（PB）。数据全部来自只增事件日志。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in system.events(kinds=("practice",)):
        groups.setdefault((e["skill_id"], e["task_key"] or "未命名"), []).append(e)
    rows = []
    for (sid, task_key), items in groups.items():
        items = sorted(items, key=lambda x: x["at"])
        rule = effective_rule(sid)
        main = rule.metrics[0].key if rule else None

        def value(e):
            return (int((e.get("metrics") or {}).get(main, e["delta"]))
                    if main else int(e["delta"]))

        first, latest = items[0], items[-1]
        best = max(items, key=value)
        rows.append({
            "skill_id": sid, "task_key": task_key, "count": len(items),
            "metric_label": rule.metrics[0].label if rule else "熟练度",
            "metric_unit": rule.metrics[0].unit if rule else "点",
            "first_value": value(first), "best_value": value(best),
            "latest_value": value(latest),
            "is_pb": best["seq"] == latest["seq"] and len(items) > 1,
            "items": [{
                "at": e["at"], "value": value(e), "delta": e["delta"],
                "quality": e["quality"], "feedback": e["feedback"],
                "note": e.get("note", ""), "seq": e["seq"],
            } for e in items[-8:]],
        })
    return sorted(rows, key=lambda r: (-r["count"], r["task_key"]))[:limit_groups]


# --------------------------------------------------------------------------
# 低摩擦采集层：候选时段只生成小票，确认后才调用 submit_practice 入账。
# --------------------------------------------------------------------------
def capture_context() -> tuple[set[int], set[str]]:
    """真实事件号集合 + 事件日志中已出现的「快速结算」标记集合。"""
    events = STATE["system"].events()
    seqs = {int(e["seq"]) for e in events}
    tags = set()
    for e in events:
        note = str(e.get("note", ""))
        if note.startswith(NOTE_TAG_PREFIX) and "]" in note:
            tags.add(note[len(NOTE_TAG_PREFIX):note.index("]")])
    return seqs, tags


def capture_payload() -> dict:
    seqs, tags = capture_context()
    return STATE["capture"].snapshot(now=STATE["clock"](),
                                     event_seqs=seqs, tagged_ids=tags)


def benchmark_payload() -> dict:
    seqs, _ = capture_context()
    snap = STATE["benchmark"].snapshot(now=STATE["clock"](), event_seqs=seqs)
    # 候选基准来自全量事件日志（不是界面只显示的近 80 条），避免旧记录选不到。
    snap["candidates"] = [
        {"seq": int(e["seq"]), "skill_id": e["skill_id"],
         "task_key": e.get("task_key") or "未命名", "at": int(e["at"])}
        for e in STATE["system"].events(kinds=("practice",))
        if e.get("feedback") == "measured"
    ][:50]
    return snap


def last_valid_event(skill_id: str) -> dict | None:
    """最近一次有效练习事件，只从只增事件日志读，不编造历史。"""
    for e in STATE["system"].events(skill_id=skill_id, kinds=("practice",)):
        if int(e["delta"]) > 0:
            return e
    return None


def submit_practice(body: dict) -> dict:
    """唯一的「加熟练度」入口：网页打卡与快速结算都走这里，判定口径完全一致。"""
    system: ProficiencySystem = STATE["system"]
    clock: SimClock = STATE["clock"]
    world: WorldState = STATE["world"]
    if not isinstance(body, dict):
        raise ValueError("请求体必须是一个对象")
    for field_name in ("skill_id", "difficulty", "quality", "feedback"):
        if field_name not in body:
            raise ValueError(f"缺少必填字段：{field_name}")
    # v7 练习问卷：先算微调系数，再走原有判定；问卷不参与有效/无效判定。
    survey_answers = body.get("survey")
    survey_info = None
    survey_bps = 10_000
    if survey_answers:
        survey_info = session_factor_bps(survey_answers)
        survey_bps = survey_info["bps"]
    seqs_before = {int(e["seq"]) for e in system.events()}
    # 同一秒内连续提交时，保证结算时间严格递增，避免 TIME_REVERSED 误判。
    last = system.get_progress(body["skill_id"], now=clock()).last_practice_at
    now = max(clock(), last + 1)
    buff_bps = world.insight_multiplier_bps()
    result = system.add_practice(
        body["skill_id"], difficulty=body["difficulty"],
        quality=body["quality"], feedback=body["feedback"],
        task_key=(str(body.get("task_key") or "").strip() or "未命名任务"),
        note=str(body.get("note", "")), now=now,
        metrics=body.get("metrics"),
        proof_type=str(body.get("proof_type", "")),
        proof_url=str(body.get("proof_url", "")),
        buff_bps=buff_bps,
        survey_bps=survey_bps,
        survey=survey_answers if survey_answers else None,
    )
    buff_used = bool(result.verdict.valid and buff_bps != 10_000)
    if buff_used:
        world.consume_insight()
    v = result.verdict
    all_events = system.events()
    seqs_after = {int(e["seq"]) for e in all_events}
    new_seq = max(seqs_after - seqs_before) if seqs_after - seqs_before else None
    benchmark_result = None
    if v.valid and new_seq is not None and STATE.get("benchmark") is not None:
        ev = next((e for e in all_events if int(e["seq"]) == new_seq), None)
        if ev is not None:
            benchmark_result = STATE["benchmark"].observe(ev, now=now)
    RECENT_VERDICTS.appendleft({
        "skill_id": body["skill_id"], "valid": v.valid,
        "reason_code": v.reason_code, "gain": v.gain,
        "challenge": v.challenge, "resumed": v.resumed,
        "detail": (f"+{v.gain} 有效经验" if v.valid else "熟练度不变")
                  + (f" · 难度匹配 {v.challenge}" if v.challenge else "")
                  + (f" · 指标门 {sum(1 for c in v.metric_checks if c.get('passed_gate'))}"
                     f"/{len(v.metric_checks)} 通过" if v.metric_checks else "")
                  + (f" · 技能系数 {v.skill_factor_bps} bps"
                     if v.skill_factor_bps != 10_000 else "")
                  + (" · 中断后恢复" if v.resumed else ""),
        "at": _iso(now),
    })
    payload = skill_payload()
    payload["verdict"] = {
        "valid": v.valid, "reason_code": v.reason_code,
        "gain": v.gain, "factors": v.factors,
        "challenge": v.challenge, "resumed": v.resumed,
        "recovery_bps": v.recovery_bps,
        "skill_factor_bps": v.skill_factor_bps,
        "metric_checks": list(v.metric_checks),
    }
    payload["reason_text"] = REASON_CODES.get(v.reason_code, "")
    payload["narrative"] = narrative_for(result, buff_used=buff_used)
    payload["before_level"] = result.before.level
    payload["after_level"] = result.after.level
    level_up = None
    if result.after.level > result.before.level:
        row = next((c for c in LEVEL_CRITERIA
                    if c["level"] == result.after.level), {})
        level_up = {
            "from": result.before.level, "to": result.after.level,
            "skill_id": body["skill_id"],
            "realm": realm_title(result.after.level),
            "name": row.get("name", ""),
            "observable": row.get("observable", ""),
        }
    payload["level_up"] = level_up
    payload["practice_event_seq"] = new_seq
    payload["benchmark_result"] = benchmark_result
    payload["survey"] = survey_info
    return payload


def surveys_payload() -> dict:
    """两张问卷的题面。前端不再硬编码选项，改题只改后端一处。"""
    return {
        "ok": True,
        "baseline_max_level": BASELINE_MAX_LEVEL,
        "baseline": baseline_questions(),
        "session": session_questions(),
        "note": {
            "baseline": ("首登自评只决定「信用起点」，最高 LV3，且不加经验；"
                         "LV4 起仍要经验与达标校验。"),
            "session": ("练习问卷只把本次增益乘 0.80–1.12，"
                        "不改变本次是否计入，也不参与门槛统计。"),
        },
    }


def apply_baseline(body: dict) -> dict:
    """首登自评入口：服务端重新算等级，不信任客户端传来的 level。"""
    system: ProficiencySystem = STATE["system"]
    if not isinstance(body, dict):
        raise ValueError("请求体必须是一个对象")
    skill_id = str(body.get("skill_id", "")).strip()
    if not skill_id:
        raise ValueError("缺少 skill_id")
    answers = body.get("answers")
    scored = baseline_level(answers)          # 未知/缺项取值直接拒绝
    level = min(BASELINE_MAX_LEVEL, int(scored["level"]))
    progress = system.apply_baseline(skill_id, level, answers=answers,
                                     now=STATE["clock"]())
    out = skill_payload()
    out["baseline_result"] = {
        **scored, "level": level,
        "progress": progress.to_dict(),
    }
    return out


def skill_payload() -> dict:
    system: ProficiencySystem = STATE["system"]
    order = {name: i for i, name in enumerate(DEFAULT_SKILLS)}
    now = STATE["clock"]()
    rows = []
    for skill_id, progress in system.list_progress(now=now).items():
        cfg = system.get_config(skill_id)
        info = system.assess(skill_id, now=now)
        rows.append({
            **progress.to_dict(),
            "rule": cfg.rule.to_dict(),
            "max_level": cfg.max_level,
            "assessment": info["criteria"],
            "next_level_criteria": info["next_level_criteria"],
            "missing": _missing_items(info),
            "window_used": bool(info["criteria"].get("window_used")),
            "fast_track": bool(info["criteria"].get("fast_track")),
        })
    for row in rows:
        row["realm"] = realm_title(row["level"])
    rows.sort(key=lambda r: (order.get(r["skill_id"], 999), r["skill_id"]))
    streak = system.streak_info(now=now)
    stats = system.stats_summary(days=30, now=now)
    events = system.events(limit=80)
    practice = system.events(kinds=("practice",))
    timeline = task_timeline(system)
    context = {
        "sessions": len(practice),
        "streak": streak.get("current_days", 0),
        "total_xp": sum(max(0, e["delta"]) for e in practice),
        "best_day_xp": max((x["xp"] for x in stats["series"]), default=0),
        "max_level": max((r["level"] for r in rows), default=1),
        # 质成就：只统计真实事件日志里出现的个人最好与基准复测通过。
        "personal_bests": sum(1 for g in timeline if g["is_pb"]),
        "benchmark_passes": STATE["benchmark"].pass_count(),
    }
    return {
        "ok": True,
        "skills": rows,
        "custom_skills": (STATE["custom"].public_list()
                          if STATE.get("custom") else []),
        "streak": streak,
        "stats": stats,
        "events": events,
        "achievements": evaluate_achievements(context),
        "achievement_context": context,
        "top_realm": realm_title(context["max_level"]),
        "guidance": guidance(system, now),
        "task_timeline": timeline,
        "shape_rules": dict(SHAPE_RULES),
        "today_xp": stats["series"][-1]["xp"] if stats["series"] else 0,
        "recent_events": list(RECENT_EVENTS),
        "recent_verdicts": list(RECENT_VERDICTS),
        "verify": system.verify(),
        "save_path": str(STATE["paths"]["save"]),
        "clock": {"unix": now, "offset_days": round(STATE["clock"].offset / DAY_SECONDS, 2)},
        "timezone": {
            "offset_seconds": SETTINGS["tz_offset_seconds"],
            "label": f"UTC{SETTINGS['tz_offset_seconds'] / 3600:+.0f}",
            "local_day": day_index(now, SETTINGS["tz_offset_seconds"]),
            "utc_day": day_index(now, 0),
        },
        "world": world_payload(),
        "capture": capture_payload(),
        "benchmark": benchmark_payload(),
        "dev_tools": bool(SETTINGS.get("dev_tools")),
        "baseline": {
            "max_level": BASELINE_MAX_LEVEL,
            # 只对「全新且没自评过」的技能提示做首登问卷；做过练习的不再问。
            "pending": [r["skill_id"] for r in rows
                        if not r.get("baseline_level")
                        and int(r.get("total_exp", 0)) == 0
                        and int(r.get("valid_sessions", 0)) == 0],
            "levels": {r["skill_id"]: int(r.get("baseline_level", 0))
                       for r in rows},
        },
    }




def handle_capture(action: str, body: dict) -> dict:
    """候选时段接口。只有 settle 且用户确认 yes 时才可能加分。"""
    ledger: CaptureLedger = STATE["capture"]
    system: ProficiencySystem = STATE["system"]
    now = STATE["clock"]()
    if action == "start":
        row = ledger.start(str(body["skill_id"]), now=now)
        out = skill_payload()
        out["capture_session"] = row
        return out
    if action == "stop":
        sid = str(body["session_id"])
        skill_id = ledger.get(sid)["skill_id"]
        level = system.get_progress(skill_id, now=now).level
        parts = custom_form_parts(skill_id)
        form_args = ({"form_rule": parts[0], "form_spec": parts[1]}
                     if parts else {})
        row = ledger.stop(sid, body.get("hidden_intervals", []),
                          last_event=last_valid_event(skill_id),
                          note=str(body.get("note", "")),
                          level=level, now=now, **form_args)
        out = skill_payload()
        out["capture_session"] = row
        return out
    if action == "discard":
        row = ledger.discard(str(body["session_id"]))
        out = skill_payload()
        out["capture_session"] = row
        return out
    if action == "settle":
        def practice_fn(practice_body: dict) -> dict:
            p = submit_practice(practice_body)
            return {
                "payload": p,
                "event_seq": p.get("practice_event_seq"),
                "valid": bool(p["verdict"]["valid"]),
                "reason_code": str(p["verdict"]["reason_code"]),
            }

        row, inner = ledger.settle(
            str(body["session_id"]), body.get("answers") or {},
            practice_fn, now=now,
        )
        out = inner if inner else skill_payload()
        out["capture_session"] = row
        # 结算状态在 submit_practice 之后才落定，这里刷新一次小票快照。
        out["capture"] = capture_payload()
        return out
    raise ValueError(f"未知采集操作 {action!r}")


class Handler(BaseHTTPRequestHandler):
    server_version = "ProficiencyWeb/2.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[web] " + (fmt % args) + "\n")

    def _send(self, code: int, payload, ctype: str = "application/json; charset=utf-8") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _read_json(self, max_bytes: int = 64 * 1024) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > max_bytes:
            raise ValueError(f"请求体为空或超过 {max_bytes} 字节")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        with API_LOCK:
            try:
                system: ProficiencySystem = STATE["system"]
                clock: SimClock = STATE["clock"]

                if method == "GET" and path == "/":
                    self._send(200, HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if method == "GET" and path == "/favicon.ico":
                    self._send(204, b"")
                    return
                if method == "GET" and path == "/api/health":
                    self._send(200, {
                        "ok": True, "version": SCHEMA_VERSION,
                        "skill_count": len(system.list_progress(now=clock())),
                        "verify": system.verify(), "save_path": str(STATE["paths"]["save"]),
                        "clock_unix": clock(),
                        "world_verify": STATE["world"].verify(),
                        "reputation": STATE["world"].available_reputation(),
                        "capture_verify": STATE["capture"].verify(
                            *capture_context()),
                        "benchmark_verify": STATE["benchmark"].verify(
                            capture_context()[0]),
                        "custom_verify": (STATE["custom"].verify()
                                         if STATE.get("custom") else []),
                        "tz_offset_seconds": SETTINGS["tz_offset_seconds"],
                    })
                    return
                if method == "GET" and path == "/api/skills":
                    self._send(200, skill_payload())
                    return
                if method == "GET" and path == "/api/surveys":
                    self._send(200, surveys_payload())
                    return
                if method == "POST" and path == "/api/baseline":
                    self._send(200, apply_baseline(self._read_json()))
                    return
                if path == "/api/skills/custom" and method == "GET":
                    self._send(200, custom_skills_payload())
                    return
                if path == "/api/skills/custom/add" and method == "POST":
                    self._send(200, add_custom_skill(self._read_json()))
                    return
                if path == "/api/skills/custom/remove" and method == "POST":
                    self._send(200, remove_custom_skill(self._read_json()))
                    return
                if method == "GET" and path == "/api/rules":
                    self._send(200, rules_payload())
                    return
                if method == "GET" and path == "/api/decisions":
                    self._send(200, decisions_payload())
                    return
                if method == "GET" and path == "/api/world":
                    self._send(200, world_payload())
                    return
                if method == "GET" and path == "/api/settings":
                    self._send(200, {
                        "ok": True, **SETTINGS,
                        "system_tz_offset_seconds":
                            system.timezone_offset_seconds,
                        "save_path": str(STATE["paths"]["settings"]),
                        "note": ("日历口径统一用于同日递减、连胜、今日所得、"
                                 "每日声望上限与近 30 日统计；改动会按事件日志"
                                 "重建连胜，不改变累计经验与事件日志。"),
                    })
                    return
                if method == "GET" and path == "/api/export":
                    bundle = export_bundle(STATE["paths"]["data_dir"], now=clock())
                    bundle["ok"] = True
                    bundle["files_list"] = sorted(bundle["files"])
                    self._send(200, bundle)
                    return
                if method == "POST" and path == "/api/import":
                    body = self._read_json(32 * 1024 * 1024)
                    bundle = body.get("bundle", body)
                    validate_bundle(bundle)
                    # 覆盖前先留一份可回退的现场备份。
                    before = make_backup(STATE["paths"]["data_dir"], now=clock(),
                                         tag="before-import")
                    written = write_bundle(STATE["paths"]["data_dir"], bundle)
                    reload_state()
                    out = skill_payload()
                    out["import_result"] = {
                        "ok": True, "written": written,
                        "backup_file": before,
                        "verify": out["verify"] + out["capture"]["verify"] +
                                  out["benchmark"]["verify"],
                    }
                    self._send(200, out)
                    return
                if method == "POST" and path == "/api/backup":
                    self._read_json()
                    name = make_backup(STATE["paths"]["data_dir"], now=clock(),
                                       tag="manual")
                    self._send(200, {"ok": True, "backup_file": name,
                                     "backup_dir": str(STATE["paths"]["data_dir"] / "backups")})
                    return
                if method == "GET" and path == "/api/backups":
                    folder = STATE["paths"]["data_dir"] / "backups"
                    rows = sorted((p.name for p in folder.glob("*.zip")),
                                  reverse=True) if folder.exists() else []
                    self._send(200, {"ok": True, "backups": rows,
                                     "keep_days": 14,
                                     "backup_dir": str(folder)})
                    return
                if method == "POST" and path == "/api/settings":
                    from realistic_rules import validate_tz_offset
                    body = self._read_json()
                    changed = False
                    if "dev_tools" in body:
                        # 开发者工具（时间旅行 / 改曲线 / 直注经验）默认关闭，
                        # 必须由用户在本机设置页显式打开；公网部署请保持关闭。
                        SETTINGS["dev_tools"] = bool(body["dev_tools"])
                        changed = True
                    if "tz_offset_seconds" in body:
                        new = validate_tz_offset(int(body["tz_offset_seconds"]))
                        old = SETTINGS["tz_offset_seconds"]
                        if new != old:
                            system.set_timezone_offset(new, now=clock())
                            STATE["world"] = WorldState(
                                STATE["paths"]["world"], created_at=clock(),
                                tz_offset_seconds=new)
                            SETTINGS["tz_offset_seconds"] = new
                            changed = True
                    if changed:
                        save_settings(dict(SETTINGS))
                    self._send(200, skill_payload())
                    return
                if method == "GET" and path == "/api/assess":
                    sid = (query.get("skill") or [""])[0]
                    self._send(200, {"ok": True, **system.assess(sid, now=clock())})
                    return

                if method == "POST" and path == "/api/practice":
                    self._send(200, submit_practice(self._read_json()))
                    return

                if method == "GET" and path == "/api/capture":
                    self._send(200, {
                        "ok": True, **capture_payload(),
                        "low_friction": dict(LOW_FRICTION_RULES),
                        "capability_verification": [
                            dict(x) for x in CAPABILITY_VERIFICATION],
                    })
                    return

                if method == "POST" and path == "/api/capture/start":
                    self._send(200, handle_capture("start", self._read_json()))
                    return
                if method == "POST" and path == "/api/capture/stop":
                    self._send(200, handle_capture("stop", self._read_json()))
                    return
                if method == "POST" and path == "/api/capture/settle":
                    self._send(200, handle_capture("settle", self._read_json()))
                    return
                if method == "POST" and path == "/api/capture/discard":
                    self._send(200, handle_capture("discard", self._read_json()))
                    return

                if method == "GET" and path == "/api/benchmark":
                    self._send(200, benchmark_payload())
                    return
                if method == "POST" and path == "/api/benchmark/create":
                    body = self._read_json()
                    seq = int(body["event_seq"])
                    ev = next((e for e in system.events()
                               if int(e["seq"]) == seq), None)
                    if ev is None:
                        raise ValueError(f"事件号 {seq} 不存在")
                    card = STATE["benchmark"].create(
                        ev, now=clock(),
                        min_gap_days=int(body.get("min_gap_days", 28)))
                    out = skill_payload()
                    out["benchmark_card"] = card
                    self._send(200, out)
                    return
                if method == "POST" and path == "/api/benchmark/deactivate":
                    body = self._read_json()
                    card = STATE["benchmark"].deactivate(str(body["card_id"]))
                    out = skill_payload()
                    out["benchmark_card"] = card
                    self._send(200, out)
                    return

                if method == "POST" and path in (
                        "/api/advance", "/api/config", "/api/exp", "/api/reset") \
                        and not SETTINGS.get("dev_tools"):
                    raise ValueError("开发者工具未开启：请先在「设置」里打开，"
                                     "再使用时间旅行 / 曲线调整 / 直接注入经验。")

                if method == "POST" and path == "/api/advance":
                    body = self._read_json()
                    days = body.get("days")
                    if isinstance(days, bool) or not isinstance(days, (int, float)) \
                            or days <= 0 or days > 3650:
                        raise ValueError("days 必须是 0..3650 之间的正数")
                    clock.advance_days(int(days))
                    now = clock()
                    for sid in list(system.list_progress(now=now)):
                        system.settle(sid, now=now)
                    system.flush()
                    self._send(200, skill_payload())
                    return

                if method == "POST" and path == "/api/config":
                    body = self._read_json()
                    rule = PowerRule(base=int(body["base"]),
                                     exponent=str(body["param"]))
                    system.configure(body["skill_id"],
                                     SkillConfig(rule, int(body["max_level"])))
                    self._send(200, skill_payload())
                    return

                if method == "POST" and path == "/api/exp":
                    # 兼容旧接口：直接注入有效经验，等价于一次有效练习
                    body = self._read_json()
                    system.add_exp(body["skill_id"], body["amount"],
                                   reason="网页快捷注入", now=clock())
                    self._send(200, skill_payload())
                    return

                if method == "POST" and path == "/api/altruism":
                    body = self._read_json()
                    action = str(body.get("action", ""))
                    now = clock()
                    w: WorldState = STATE["world"]
                    all_seqs, practice_seqs = event_seq_sets()
                    note = str(body.get("note", ""))[:120]
                    if action == "guard":
                        res = w.guard(str(body.get("target", "")),
                                      now=now, note=note)
                    elif action == "encourage":
                        res = w.encourage(int(body.get("target")), now=now,
                                          event_seqs=all_seqs, note=note)
                    elif action == "share":
                        res = w.share(int(body.get("target")), now=now,
                                      event_seqs=all_seqs,
                                      practice_seqs=practice_seqs, note=note)
                    elif action == "exchange":
                        res = w.exchange(str(body.get("item", "")), now=now)
                    elif action == "use_insight":
                        res = w.use_insight(now=now)
                    elif action == "use_freeze":
                        res = w.use_freeze(now=now)
                        if res.get("ok"):
                            system.grant_freeze(1)
                            res["reason_code"] = "FREEZE_GRANTED"
                    else:
                        raise ValueError(f"未知利他操作 {action!r}")
                    payload = skill_payload()
                    payload["action"] = action
                    payload["action_result"] = res
                    payload["reason_text"] = ALTRUISM_REASON_CODES.get(
                        str(res.get("reason_code", "")), ""
                    )
                    self._send(200, payload)
                    return

                if method == "POST" and path == "/api/decisions":
                    body = self._read_json()
                    if body.get("restore"):
                        # 服务端按规则规格恢复，不信任前端传来的批量值；
                        # 不提供「清空」入口，避免拍板记录被批量抹掉。
                        for did, key in APPLIED_DECISIONS.items():
                            _record_decision(did, key)
                    elif "selections" in body:
                        batch = body["selections"]
                        if not isinstance(batch, dict):
                            raise ValueError("selections 必须是对象")
                        for did, key in batch.items():
                            _record_decision(did, key)
                    else:
                        _record_decision(body.get("id"), body.get("key"))
                    save_decisions()
                    self._send(200, decisions_payload())
                    return

                if method == "POST" and path == "/api/reset":
                    self._read_json()
                    clock.reset()
                    now = clock()
                    for sid in list(system.list_progress(now=now)):
                        system.reset_skill(sid, now=now)  # 清经验、统计与任务指纹
                    for name, cfg in DEFAULT_SKILLS.items():
                        system.configure(name, cfg)
                    system.reset_streak()  # 进度清零时连胜也归零；事件日志按只增保留
                    system.flush()
                    STATE["capture"].clear_all()  # 候选小票一并清空，不保留待确认项
                    STATE["benchmark"].clear_all()  # 基准卡与复测证据线一并清空
                    RECENT_EVENTS.clear()
                    RECENT_VERDICTS.clear()
                    self._send(200, skill_payload())
                    return

                self._send(404, {"ok": False, "error": f"未知接口 {method} {path}"})
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                # 业务校验失败只回人话，不再把异常类型名和 JSON 细节丢给前端。
                msg = str(exc) if isinstance(exc, ValueError) \
                    else f"请求无效：{exc}"
                self._send(400, {"ok": False, "error": msg})
            except Exception as exc:  # noqa: BLE001 - 服务端兜底并打日志
                traceback.print_exc()
                self._send(500, {"ok": False,
                                 "error": "服务端异常，详情见服务端日志"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        """预览面板与探活会发 HEAD；返回与 GET / 相同的头，不写 body。"""
        payload = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_POST(self) -> None:
        self._handle("POST")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"熟练度系统网页版已启动: {url}")
    print(f"存档位置: {STATE["paths"]["save"]}")
    print("按 Ctrl+C 停止服务")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE["system"].flush()
        server.server_close()
        print("已停止，进度已写入存档。")


if __name__ == "__main__":
    main()
