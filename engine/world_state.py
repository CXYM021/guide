"""
利他层（Altruism Layer）—— 声望 / 护道 / 悟道卡 / 连胜激冻。

诚实边界（与代码同等重要，不得删除或淡化）
--------------------------------------------------
1. 本模块是【本机多角色模拟】，不联网、没有真实社区、没有其他使用者。
   「同门」是本机预置档案；声望只在本机可见，**不具备任何社会可见性**。
   数据结构按 actor_id 设计，仅为将来接入真实社区预留，当前不连接任何外部服务。
2. 声望汇率、兑换门槛、卡效果、每日上限全部是【本系统自定的 MVP 参数】，
   不是学术框架的实现。「GP-AIR」经联网核实查无此名称；真实存在的是
   indirect reciprocity（间接互惠）这一概念，本模块只借其「帮助他人可积累
   可共享声望」的思路，不从任何论文推导数值。
3. 悟道卡是唯一能改变单次增益的机制，必须「声望兑换 → 显式使用 → 只作用一次」，
   默认不生效，因此不改变已拍板的 12 项数值规则。声望不能购买，只能靠
   锚定真实对象的利他行为获得。
4. 利他行为必须锚定真实存在的对象：同门档案的真实连胜风险、真实的事件序号。
   不接受凭空声明「我帮助了别人」。

Python 3.10+，仅标准库。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from realistic_rules import (
    ALTRUISM_RULES, REPUTATION_TITLES,
    day_index as _rules_day_index, validate_tz_offset,
)

__all__ = [
    "WORLD_SCHEMA_VERSION", "ALTRUISM_RULES", "REPUTATION_TITLES",
    "WorldState", "day_index", "reputation_title", "at_risk",
    "evaluate_deed", "grant_freeze", "advance_streak_with_freeze",
]

WORLD_SCHEMA_VERSION = 1

# 声望汇率、兑换门槛、卡效果与每日上限的唯一来源是
# realistic_rules.ALTRUISM_RULES；本模块不另写一份，避免两份口径。
DAY_SECONDS = 86_400


def day_index(unix_seconds: int, tz_offset_seconds: int = 0) -> int:
    """日历天序号，统一委托给 realistic_rules.day_index（默认 UTC 兼容）。"""
    return _rules_day_index(unix_seconds, tz_offset_seconds)


def reputation_title(reputation: int) -> str:
    title = REPUTATION_TITLES[0]["name"]
    for row in REPUTATION_TITLES:
        if int(reputation) >= int(row["min"]):
            title = str(row["name"])
    return title


def at_risk(actor: dict, now: int, tz_offset_seconds: int = 0) -> bool:
    """同门连胜是否处于「今天再不练就要断」的可计算状态。纯函数。"""
    last = actor.get("last_practice_day")
    if last is None:
        return False
    return day_index(now, tz_offset_seconds) - int(last) >= 1


def reputation_title_ladder() -> list[dict]:
    return [dict(x) for x in REPUTATION_TITLES]


# --------------------------------------------------------------------------
# 纯函数：利他行为判定（先命中先返回，全部可复现）
# --------------------------------------------------------------------------

def evaluate_deed(*, kind: str, actor_exists: bool,
                  target_exists: bool, target_at_risk: bool,
                  event_exists: bool, event_is_practice: bool,
                  dedupe_hit: bool, day_reputation: int,
                  daily_cap: int) -> dict:
    """判定一次利他行为是否可计入声望。纯函数，不读时钟、不改状态。

    返回 {"valid": bool, "reason_code": str, "reputation": int}。
    无效行为一律 0 声望、零副作用。
    """
    table = ALTRUISM_RULES["reputation"]
    if kind not in table:
        return {"valid": False, "reason_code": "UNKNOWN_DEED", "reputation": 0}
    if not actor_exists:
        return {"valid": False, "reason_code": "UNKNOWN_ACTOR", "reputation": 0}
    if dedupe_hit:
        return {"valid": False, "reason_code": "DUPLICATE", "reputation": 0}
    if kind == "guard":
        if not target_exists:
            return {"valid": False, "reason_code": "UNKNOWN_TARGET", "reputation": 0}
        if not target_at_risk:
            return {"valid": False, "reason_code": "TARGET_NOT_AT_RISK", "reputation": 0}
    else:
        if not event_exists:
            return {"valid": False, "reason_code": "UNKNOWN_EVENT", "reputation": 0}
        if kind == "share" and not event_is_practice:
            return {"valid": False, "reason_code": "EVENT_NOT_PRACTICE", "reputation": 0}
    room = max(0, int(daily_cap) - int(day_reputation))
    gain = min(int(table[kind]), room)
    if gain <= 0:
        return {"valid": False, "reason_code": "DAILY_CAP", "reputation": 0}
    return {"valid": True, "reason_code": "OK", "reputation": gain}


def grant_freeze(streak_state: dict) -> dict:
    """给连胜状态装备一张「连胜激冻」。纯函数。"""
    out = dict(streak_state)
    out["freeze_charges"] = int(out.get("freeze_charges", 0)) + 1
    return out


def advance_streak_with_freeze(state: dict, today_unix: int) -> dict:
    """推进连胜（含「连胜激冻」道具）。

    判定逻辑的唯一来源是 realistic_rules.advance_streak，本函数只做转发，
    避免两份口径。优先级：同日 / 正常延续 / 免费补签 / 消耗激冻 / 降半保留。
    默认 freeze_charges=0，行为与 v3 完全一致。
    """
    from realistic_rules import advance_streak
    return advance_streak(state, today_unix)


# --------------------------------------------------------------------------
# 世界层状态与持久化
# --------------------------------------------------------------------------

class WorldState:
    """本机多角色利他层。所有写入走 _mutate()，原子落盘。

    存档独立文件（默认 web_save/world.json），与技能进度存档分离：
    技能进度是「个人数值」，世界层是「关系记录」，关注点不同；
    分开后利他逻辑出错不会损坏技能存档。
    """

    def __init__(self, path: str | os.PathLike[str], *, created_at: int | None = None,
                 tz_offset_seconds: int = 0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._created_at = int(created_at or 0)
        self._tz_offset = validate_tz_offset(tz_offset_seconds)
        self._data: dict = {}
        self._load()

    # ---------- 持久化 ----------

    def _blank(self) -> dict:
        day = day_index(self._created_at, self._tz_offset)
        actors: dict[str, dict] = {}
        for row in ALTRUISM_RULES["actors"]:
            lag = row["day_lag"]
            actors[row["id"]] = {
                "name": row["name"],
                "is_player": bool(row["is_player"]),
                "initial_streak": int(row["initial_streak"]),
                "last_practice_day": None if lag is None else day - int(lag),
                "streak_days": int(row["initial_streak"]),
                "protected_days": 0,
                "simulated": not bool(row["is_player"]),
            }
        return {
            "version": WORLD_SCHEMA_VERSION,
            "created_day": day,
            "actors": actors,
            "deeds": [],
            "wallet": {
                "reputation_spent": 0,
                "insight_cards": 0,
                "freeze_cards": 0,
                "pending_insight": False,
                "insight_used": 0,
                "freeze_used": 0,
            },
            "dedupe": [],
        }

    def _load(self) -> None:
        if not self.path.exists():
            self._data = self._blank()
            self._save()
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("world 存档根节点必须是对象")
        version = int(raw.get("version", 1))
        if version > WORLD_SCHEMA_VERSION:
            raise ValueError(
                f"world 存档 v{version} 高于本模块支持的 v{WORLD_SCHEMA_VERSION}"
            )
        blank = self._blank()
        for key in ("deeds", "dedupe"):
            raw.setdefault(key, blank[key])
        raw.setdefault("actors", blank["actors"])
        raw.setdefault("created_day", blank["created_day"])
        for key, value in blank["wallet"].items():
            raw.setdefault("wallet", {}).setdefault(key, value)
        raw["version"] = WORLD_SCHEMA_VERSION
        # 自愈：补上新增的同门档案，不覆盖已有数据
        for aid, actor in blank["actors"].items():
            raw["actors"].setdefault(aid, actor)
        self._data = raw
        if version < WORLD_SCHEMA_VERSION:
            self._save()

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(prefix="world_", suffix=".json",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _mutate(self) -> None:
        self._save()

    # ---------- 派生量 ----------

    def available_reputation(self) -> int:
        earned = sum(int(d.get("reputation", 0)) for d in self._data["deeds"])
        return earned - int(self._data["wallet"]["reputation_spent"])

    def day_reputation(self, now: int) -> int:
        day = day_index(now, self._tz_offset)
        return sum(int(d.get("reputation", 0)) for d in self._data["deeds"]
                   if day_index(int(d["at"]), self._tz_offset) == day)

    def has_pending_insight(self) -> bool:
        return bool(self._data["wallet"]["pending_insight"])

    def insight_multiplier_bps(self) -> int:
        return (int(ALTRUISM_RULES["insight_multiplier_bps"])
                if self.has_pending_insight()
                else int(ALTRUISM_RULES["base_multiplier_bps"]))

    def actor_ids(self) -> list[str]:
        return list(self._data["actors"])

    def peer_ids(self) -> list[str]:
        return [aid for aid, a in self._data["actors"].items()
                if not a.get("is_player")]

    def _dedupe_key(self, kind: str, target: str, now: int) -> str:
        if kind == "guard":
            return f"guard:{target}:{day_index(now, self._tz_offset)}"
        return f"{kind}:{target}"

    def _append_deed(self, *, kind: str, at: int, target: str,
                     reputation: int, note: str = "") -> dict:
        deeds = self._data["deeds"]
        deed = {
            "seq": len(deeds) + 1,
            "at": int(at),
            "actor_id": "player",
            "kind": kind,
            "target": target,
            "reputation": int(reputation),
            "note": str(note)[:int(ALTRUISM_RULES["max_note_len"])],
        }
        deeds.append(deed)
        self._data["dedupe"].append(self._dedupe_key(kind, target, at))
        return deed

    # ---------- 只读快照 ----------

    def snapshot(self, *, now: int, event_seqs: set[int] | None = None,
                 practice_seqs: set[int] | None = None) -> dict:
        actors = []
        for aid, a in self._data["actors"].items():
            actors.append({
                **dict(a),
                "id": aid,
                "at_risk": at_risk(a, now, self._tz_offset),
                "days_since_last": (
                    None if a.get("last_practice_day") is None
                    else day_index(now, self._tz_offset) - int(a["last_practice_day"])
                ),
            })
        rep = self.available_reputation()
        return {
            "ok": True,
            "version": WORLD_SCHEMA_VERSION,
            "scope": "local_multi_actor_simulation",
            "scope_note": (
                "本机多角色模拟：同门是本机预置档案，声望只在本机可见，"
                "不联网、没有真实社区与其他使用者。"
            ),
            "actors": actors,
            "peers": sorted(
                (a for a in actors if not a["is_player"]),
                key=lambda a: (-int(a.get("streak_days") or 0), str(a.get("name") or "")),
            ),
            "reputation": rep,
            "reputation_title": reputation_title(rep),
            "reputation_titles": reputation_title_ladder(),
            "day_reputation": self.day_reputation(now),
            "wallet": dict(self._data["wallet"]),
            "deeds": [dict(d) for d in reversed(self._data["deeds"][-40:])],
            "rules": {
                "reputation": dict(ALTRUISM_RULES["reputation"]),
                "daily_reputation_cap": ALTRUISM_RULES["daily_reputation_cap"],
                "exchange_cost": dict(ALTRUISM_RULES["exchange_cost"]),
                "insight_multiplier_bps": ALTRUISM_RULES["insight_multiplier_bps"],
                "no_purchase": True,
            },
            "guild_qi": sum(int(a["streak_days"]) for a in actors),
            "event_seqs": sorted(event_seqs or set())[-40:],
            "practice_seqs": sorted(practice_seqs or set())[-40:],
            "verify": self.verify(),
        }

    def verify(self) -> list[str]:
        issues: list[str] = []
        w = self._data["wallet"]
        rep = self.available_reputation()
        if rep < 0:
            issues.append(f"声望为负：{rep}")
        for key in ("insight_cards", "freeze_cards", "reputation_spent",
                    "insight_used", "freeze_used"):
            if int(w.get(key, 0)) < 0:
                issues.append(f"钱包 {key} 为负")
            if int(w.get(key, 0)) > 100_000:
                issues.append(f"钱包 {key} 数值异常")
        for i, d in enumerate(self._data["deeds"], 1):
            if int(d["seq"]) != i:
                issues.append(f"利他事件序号不连续：第 {i} 条 seq={d['seq']}")
            if d["kind"] not in ALTRUISM_RULES["reputation"]:
                issues.append(f"未知利他类型 {d['kind']!r}")
            if int(d["reputation"]) <= 0:
                issues.append(f"第 {i} 条声望增量非正")
        keys = [self._dedupe_key(d["kind"], str(d["target"]), int(d["at"]))
                for d in self._data["deeds"]]
        if len(keys) != len(set(keys)):
            issues.append("利他事件存在重复键（防刷失效）")
        if set(keys) - set(self._data["dedupe"]):
            issues.append("防刷索引缺项")
        if len(self._data["deeds"]) > int(ALTRUISM_RULES["max_deeds"]):
            issues.append("利他日志过长，建议归档")
        return issues

    # ---------- 写入 ----------

    def guard(self, target: str, *, now: int, note: str = "") -> dict:
        """护道：为「连胜确实处于风险中」的同门提供一次保护。"""
        actors = self._data["actors"]
        actor = actors.get(target)
        verdict = evaluate_deed(
            kind="guard", actor_exists="player" in actors,
            target_exists=actor is not None and not actor.get("is_player"),
            target_at_risk=bool(actor and at_risk(actor, now, self._tz_offset)),
            event_exists=False, event_is_practice=False,
            dedupe_hit=self._dedupe_key("guard", str(target), now) in self._data["dedupe"],
            day_reputation=self.day_reputation(now),
            daily_cap=int(ALTRUISM_RULES["daily_reputation_cap"]),
        )
        if not verdict["valid"]:
            return {"ok": False, "reason_code": verdict["reason_code"],
                    "reputation": 0}
        deed = self._append_deed(kind="guard", at=now, target=target,
                                 reputation=verdict["reputation"], note=note)
        actor["last_practice_day"] = day_index(now, self._tz_offset)
        actor["protected_days"] = int(actor.get("protected_days", 0)) + 1
        self._mutate()
        return {"ok": True, "reason_code": "OK", "deed": deed,
                "reputation": verdict["reputation"]}

    def encourage(self, event_seq: int, *, now: int,
                  event_seqs: set[int], note: str = "") -> dict:
        """题签：对一条【真实存在的事件】留下鼓励。"""
        seq = int(event_seq)
        verdict = evaluate_deed(
            kind="encourage", actor_exists="player" in self._data["actors"],
            target_exists=False, target_at_risk=False,
            event_exists=seq in event_seqs, event_is_practice=False,
            dedupe_hit=self._dedupe_key("encourage", str(seq), now) in self._data["dedupe"],
            day_reputation=self.day_reputation(now),
            daily_cap=int(ALTRUISM_RULES["daily_reputation_cap"]),
        )
        if not verdict["valid"]:
            return {"ok": False, "reason_code": verdict["reason_code"],
                    "reputation": 0}
        deed = self._append_deed(kind="encourage", at=now, target=str(seq),
                                 reputation=verdict["reputation"], note=note)
        self._mutate()
        return {"ok": True, "reason_code": "OK", "deed": deed,
                "reputation": verdict["reputation"]}

    def share(self, event_seq: int, *, now: int,
              event_seqs: set[int], practice_seqs: set[int],
              note: str = "") -> dict:
        """传承：把一条【真实有效练习】公开为他人的参考范例。"""
        seq = int(event_seq)
        verdict = evaluate_deed(
            kind="share", actor_exists="player" in self._data["actors"],
            target_exists=False, target_at_risk=False,
            event_exists=seq in event_seqs,
            event_is_practice=seq in practice_seqs,
            dedupe_hit=self._dedupe_key("share", str(seq), now) in self._data["dedupe"],
            day_reputation=self.day_reputation(now),
            daily_cap=int(ALTRUISM_RULES["daily_reputation_cap"]),
        )
        if not verdict["valid"]:
            return {"ok": False, "reason_code": verdict["reason_code"],
                    "reputation": 0}
        deed = self._append_deed(kind="share", at=now, target=str(seq),
                                 reputation=verdict["reputation"], note=note)
        self._mutate()
        return {"ok": True, "reason_code": "OK", "deed": deed,
                "reputation": verdict["reputation"]}

    def exchange(self, kind: str, *, now: int) -> dict:
        """声望兑换。只能兑换，不提供任何购买入口。"""
        costs = ALTRUISM_RULES["exchange_cost"]
        if kind not in costs:
            return {"ok": False, "reason_code": "UNKNOWN_ITEM"}
        cost = int(costs[kind])
        w = self._data["wallet"]
        key = "insight_cards" if kind == "insight_card" else "freeze_cards"
        if int(w[key]) >= int(ALTRUISM_RULES["max_cards"]):
            return {"ok": False, "reason_code": "CARD_LIMIT"}
        if self.available_reputation() < cost:
            return {"ok": False, "reason_code": "NOT_ENOUGH_REPUTATION"}
        w["reputation_spent"] = int(w["reputation_spent"]) + cost
        w[key] = int(w[key]) + 1
        self._mutate()
        return {"ok": True, "reason_code": "OK", "item": kind, "cost": cost,
                "wallet": dict(w)}

    def use_insight(self, *, now: int) -> dict:
        """显式使用悟道卡 → 装备一个「下一次有效练习翻倍」的待用效果。"""
        w = self._data["wallet"]
        if int(w["insight_cards"]) <= 0:
            return {"ok": False, "reason_code": "NO_CARD"}
        if w["pending_insight"]:
            return {"ok": False, "reason_code": "ALREADY_ARMED"}
        w["insight_cards"] = int(w["insight_cards"]) - 1
        w["pending_insight"] = True
        w["insight_used"] = int(w["insight_used"]) + 1
        self._mutate()
        return {"ok": True, "reason_code": "OK",
                "multiplier_bps": int(ALTRUISM_RULES["insight_multiplier_bps"])}

    def consume_insight(self) -> dict:
        """有效练习成功后消耗待用效果；无效练习不得调用。"""
        w = self._data["wallet"]
        if not w["pending_insight"]:
            return {"ok": False, "reason_code": "NOT_ARMED"}
        w["pending_insight"] = False
        self._mutate()
        return {"ok": True, "reason_code": "OK"}

    def use_freeze(self, *, now: int) -> dict:
        """装备连胜激冻。返回 freeze_granted 由服务层写入连胜状态。"""
        w = self._data["wallet"]
        if int(w["freeze_cards"]) <= 0:
            return {"ok": False, "reason_code": "NO_CARD"}
        w["freeze_cards"] = int(w["freeze_cards"]) - 1
        w["freeze_used"] = int(w["freeze_used"]) + 1
        self._mutate()
        return {"ok": True, "reason_code": "OK", "freeze_granted": True}
