# -*- coding: utf-8 -*-
"""好友：同一台服务器上的**真实账号**之间互相关注（不联网、不外发）。

诚实边界（删掉就等于骗人）
--------------------------
1. 「好友」= 这台服务器上的另一个真实账号。只有多人共用一台服务（局域网模式、
   自建云主机）时才加得到人；单人本机使用时没有好友可加，页面就明说「还没有好友」，
   不拿模拟角色冒充真人。
2. 好友摘要**只读**对方存档里的两件事：最近一次练习是什么时候、今天练没练。
   不读对方练了什么、分数多少、笔记写了什么。
3. 加好友双向立即生效：同一实例上注册本身就要邀请码，已经是门槛，
   不再叠一层「申请—审批」，免得又变成要等。

数据：<data_dir>/friends.json，实例级（跨账号），单文件 + 原子写。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from realistic_rules import day_index  # noqa: E402

FORMAT = "guide-friends"
VERSION = 1
MAX_FRIENDS = 50


class FriendError(ValueError):
    """可直接返回给用户的好友错误。"""


# ---------------------------------------------------------------- 纯函数


def practice_summary(user_dir, *, now: int, tz_offset: int = 0) -> dict:
    """只读对方 skills.json，回答「最近一次练习」与「今天练没练」。"""
    path = Path(user_dir) / "skills.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"known": False}
    events = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(events, list):
        return {"known": False}
    stamps = [int(e["at"]) for e in events
              if isinstance(e, dict) and isinstance(e.get("at"), int)]
    today = day_index(now, tz_offset)
    if not stamps:
        return {"known": True, "last_at": None, "days_since": None,
                "practiced_today": False, "event_count": 0}
    last = max(stamps)
    return {
        "known": True,
        "last_at": last,
        "days_since": today - day_index(last, tz_offset),
        "practiced_today": any(day_index(t, tz_offset) == today for t in stamps),
        "event_count": len(stamps),
    }


def needs_a_nudge(summary: dict) -> bool:
    """值得提醒的条件：读过对方存档、今天还没练、而且不是刚注册的空账号。"""
    if not summary.get("known") or summary.get("last_at") is None:
        return False
    if summary.get("practiced_today"):
        return False
    return int(summary.get("days_since") or 0) >= 1


# ---------------------------------------------------------------- 存储


class FriendStore:
    def __init__(self, data_dir) -> None:
        self.path = Path(data_dir) / "friends.json"
        self._lock = threading.RLock()
        self._data = {"format": FORMAT, "version": VERSION, "links": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("format") != FORMAT:
                return
            links = raw.get("links")
            if isinstance(links, dict):
                self._data = {"format": FORMAT, "version": VERSION,
                              "links": {str(k): dict(v) for k, v in links.items()
                                        if isinstance(v, dict)}}
        except (OSError, ValueError):
            self._data = {"format": FORMAT, "version": VERSION, "links": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="friends_", suffix=".json",
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

    # ---------- 读写 ----------

    def links_of(self, uid: str) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in (self._data["links"].get(uid) or {}).items()}

    def add(self, a_uid: str, a_name: str, b_uid: str, b_name: str,
            now: int | None = None) -> dict:
        if not b_uid or b_uid == a_uid:
            raise FriendError("不能把自己加成好友")
        at = int(now if now is not None else time.time())
        with self._lock:
            links = self._data["links"]
            mine = links.setdefault(a_uid, {})
            if b_uid not in mine and len(mine) >= MAX_FRIENDS:
                raise FriendError(f"好友上限 {MAX_FRIENDS} 个")
            mine[b_uid] = {"name": b_name, "at": at}
            links.setdefault(b_uid, {})[a_uid] = {"name": a_name, "at": at}
            self._save()
        return {"ok": True, "uid": b_uid, "name": b_name, "at": at}

    def remove(self, a_uid: str, b_uid: str) -> dict:
        with self._lock:
            links = self._data["links"]
            mine = links.get(a_uid) or {}
            if b_uid not in mine:
                raise FriendError("你们还不是好友")
            mine.pop(b_uid, None)
            (links.get(b_uid) or {}).pop(a_uid, None)
            self._save()
        return {"ok": True, "uid": b_uid}

    def snapshot(self, uid: str, *, users_root: Path, now: int,
                 tz_offset: int = 0) -> list[dict]:
        """好友卡片：对方名字 + 最近练习 + 今天练没练 + 值不值得提醒。"""
        out = []
        for fid, meta in self.links_of(uid).items():
            summary = practice_summary(Path(users_root) / fid,
                                       now=now, tz_offset=tz_offset)
            out.append({
                "uid": fid,
                "name": str(meta.get("name") or fid),
                "since": int(meta.get("at") or 0),
                "summary": summary,
                "needs_nudge": needs_a_nudge(summary),
            })
        out.sort(key=lambda x: (str(x["name"])))
        return out
