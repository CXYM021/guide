# -*- coding: utf-8 -*-
"""每用户独立的自定义技能账本（文件级隔离，与内置 7 技能分开存）。

存放 custom_skills.json：用户新增技能的完整规则规格 + 来源记录。
规则对象由规格在进程内构造，注入到该用户的 ProficiencySystem 实例；
不修改模块级 SKILL_RULES，所以 A 用户加的技能不会出现在 B 用户那里。
"""

from __future__ import annotations

import json
import sys
import threading
import unicodedata
import time
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from realistic_rules import (  # noqa: E402
    MAX_LEVEL, SKILL_IDS, MetricSpec, SkillRule,
)

FORMAT = "proficiency-custom-skills"
VERSION = 1
MAX_CUSTOM_SKILLS = 20


class CustomSkillError(ValueError):
    """自定义技能操作失败，可直接返回给用户。"""


def build_rule(spec: dict) -> SkillRule:
    """把落盘规格构造成不可变 SkillRule；dataclass 会做全部数值校验。"""
    metrics = []
    for m in spec.get("metrics", []):
        metrics.append(MetricSpec(
            key=m["key"], label=m["label"], unit=m["unit"],
            min_valid=int(m["min_valid"]), good=int(m["good"]),
            range_low=int(m["range_low"]), range_high=int(m["range_high"]),
            level_requirements=tuple(int(x) for x in m["level_requirements"]),
            basis=str(m.get("basis") or "draft"),
            source_ids=tuple(str(x) for x in (m.get("source_ids") or [])),
            note=str(m.get("note") or ""),
        ))
    observables = tuple(str(x) for x in spec.get("level_observables", []))
    if len(observables) != MAX_LEVEL:
        raise CustomSkillError(f"必须给 {MAX_LEVEL} 条等级描述")
    return SkillRule(
        skill_id=str(spec["skill_id"]),
        description=str(spec.get("description") or ""),
        metrics=tuple(metrics),
        level_observables=observables,
        standard=str(spec.get("standard") or "自建标准·非官方"),
        source_ids=tuple(str(x) for x in (spec.get("source_ids") or [])),
        evidence_type="draft",
        official_max_level=MAX_LEVEL,
        medical_gate=False,
    )


def settlement_spec_for(spec: dict) -> dict:
    """给自定义技能生成通用结算问卷规格（不写全局 SETTLEMENT_FORMS）。"""
    metrics = {}
    for m in spec.get("metrics", []):
        metrics[m["key"]] = {
            "label": f"本次{m['label']}（{m['unit']}）",
            "hint": (f"≥{m['min_valid']} 才算有效练习，≥{m['good']} 有加成；"
                     f"合法范围 {m['range_low']}–{m['range_high']}。"),
            "value_labels": {},
        }
    return {
        "metrics": metrics,
        "task_options": ["本次练习", "专项训练", "复盘巩固"],
        "task_label": "这次做的是什么任务？",
        "difficulty_label": "这次任务对你有多难？",
        "note_placeholder": "随手记一笔，方便以后纵向对比",
        "intro": (f"「{spec.get('skill_id', '')}」的结算问卷：数值全部是你自报，"
                  "不是仪器测量；确认后仍走原有全部判定门。"),
    }


class CustomSkillRegistry:
    def __init__(self, path, *, clock=None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: int(time.time()))
        self._lock = threading.RLock()
        self._data: dict = {"format": FORMAT, "version": VERSION, "skills": {}}
        self._load_error = ""
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("format") != FORMAT or int(raw.get("version", 0)) != VERSION:
                raise ValueError("格式或版本不匹配")
            skills = raw.get("skills")
            if not isinstance(skills, dict):
                raise ValueError("skills 必须是对象")
            for sid, spec in skills.items():
                build_rule({**spec, "skill_id": sid})
            self._data = {"format": FORMAT, "version": VERSION,
                          "skills": dict(skills)}
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            # 不因为一个坏文件就让整个服务起不来；错误进 verify() 让人看见。
            self._load_error = f"{type(exc).__name__}: {exc}"

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._data["skills"])

    def specs(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._data["skills"].items()}

    def rules(self) -> dict[str, SkillRule]:
        with self._lock:
            return {sid: build_rule({**spec, "skill_id": sid})
                    for sid, spec in self._data["skills"].items()}

    def form_for(self, skill_id: str):
        """挂机结算用：返回 (规则, 问卷规格)，非自定义技能返回 None。"""
        with self._lock:
            spec = self._data["skills"].get(skill_id)
        if spec is None:
            return None
        return (build_rule({**spec, "skill_id": skill_id}),
                settlement_spec_for({**spec, "skill_id": skill_id}))

    def add(self, spec: dict) -> dict:
        sid = str(spec.get("skill_id") or "").strip()
        if not sid:
            raise CustomSkillError("技能名不能为空")
        if sid in SKILL_IDS:
            raise CustomSkillError("内置技能不能重复添加；可直接练习或改成长曲线")
        folded = unicodedata.normalize("NFKC", sid).casefold()
        if any(folded == unicodedata.normalize("NFKC", b).casefold()
               for b in SKILL_IDS):
            raise CustomSkillError("与内置技能同名（忽略大小写或全半角）")
        build_rule({**spec, "skill_id": sid})  # 先校验，不通过不落盘
        with self._lock:
            for existing in self._data["skills"]:
                if folded == unicodedata.normalize("NFKC", existing).casefold():
                    raise CustomSkillError("已有同名技能（忽略大小写或全半角）")
            if len(self._data["skills"]) >= MAX_CUSTOM_SKILLS:
                raise CustomSkillError(
                    f"自定义技能最多 {MAX_CUSTOM_SKILLS} 个，请先删掉不用的")
            self._data["skills"][sid] = dict(spec)
            self._write()
        return dict(spec)

    def remove(self, skill_id: str) -> None:
        sid = str(skill_id or "").strip()
        if sid in SKILL_IDS:
            raise CustomSkillError("内置技能不能删除")
        with self._lock:
            if sid not in self._data["skills"]:
                raise CustomSkillError("找不到这个自定义技能")
            del self._data["skills"][sid]
            self._write()

    def verify(self) -> list[str]:
        issues: list[str] = []
        if self._load_error:
            issues.append(f"自定义技能账本读取失败：{self._load_error}")
        with self._lock:
            items = dict(self._data["skills"])
        for sid, spec in items.items():
            if sid in SKILL_IDS:
                issues.append(f"{sid}: 自定义账本里出现内置技能名")
            try:
                build_rule({**spec, "skill_id": sid})
            except (KeyError, TypeError, ValueError) as exc:
                issues.append(f"{sid}: 规格不合法（{type(exc).__name__}: {exc}）")
        return issues

    def public_list(self) -> list[dict]:
        rows = []
        for sid, spec in self.specs().items():
            p = spec.get("provenance") or {}
            rows.append({
                "skill_id": sid,
                "description": spec.get("description", ""),
                "standard": spec.get("standard", ""),
                "metric_count": len(spec.get("metrics", [])),
                "metrics": [
                    {"key": m["key"], "label": m["label"], "unit": m["unit"],
                     "min_valid": m["min_valid"], "good": m["good"],
                     "range_low": m["range_low"], "range_high": m["range_high"]}
                    for m in spec.get("metrics", [])],
                "level_requirements": [
                    m.get("level_requirements", [])
                    for m in spec.get("metrics", [])],
                "mode": p.get("mode", "local"),
                "ai_used": bool(p.get("ai_used")),
                "reason": p.get("reason", ""),
                "model": p.get("model", ""),
                "sources": p.get("sources", []),
                "verified_reference": p.get("verified_reference"),
                "created_at": spec.get("created_at", 0),
            })
        return rows
