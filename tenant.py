# -*- coding: utf-8 -*-
"""租户管理：每个用户一套独立目录、独立引擎实例、独立锁。

隔离级别是【文件级】：用户 A 的 skills.json / world.json / captures.json /
benchmarks.json / decisions.json / settings.json 全在 data/users/<A>/ 下，
API 层不接受 user_id 参数，只能从会话解析，因此无法跨租户读写。
"""

from __future__ import annotations

import re
import sys
import threading
from collections import OrderedDict
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import web_app  # noqa: E402  (路径注入后才能导入)

UID_RE = re.compile(r"^[0-9a-f]{32}$")


class TenantManager:
    """按需加载 + LRU 缓存；缓存淘汰前显式 flush，保证不丢数据。"""

    def __init__(self, data_dir: str | Path, *,
                 tz_offset_seconds: int = 28800,
                 max_cached: int = 64) -> None:
        self.root = Path(data_dir) / "users"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tz_offset_seconds = int(tz_offset_seconds)
        self.max_cached = max(1, int(max_cached))
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.RLock()
        # 锁按 user_id 常驻，不随 LRU 淘汰：即使某用户曾出现两个引擎实例，
        # 同一用户的读写也走同一把锁，杜绝 Windows 上 os.replace 互相踩踏。
        self._user_locks: dict[str, object] = {}

    @staticmethod
    def _check(uid: str) -> str:
        if not isinstance(uid, str) or not UID_RE.match(uid):
            raise ValueError("非法的用户标识")
        return uid

    def user_dir(self, uid: str) -> Path:
        return self.root / self._check(uid)

    def get(self, uid: str) -> dict:
        uid = self._check(uid)
        with self._lock:
            if uid in self._cache:
                tenant = self._cache[uid]
                self._cache.move_to_end(uid)
            else:
                tenant = web_app.make_tenant(
                    self.user_dir(uid),
                    tz_offset_seconds=self.tz_offset_seconds)
                tenant["lock"] = self._user_locks.setdefault(
                    uid, threading.RLock())
                tenant["user_id"] = uid
                # 关键：新实例先记 1 个在途引用再进缓存，否则下面的淘汰
                # 可能把「刚建好、还没交给请求线程」的实例淘汰掉，
                # 下次 get 又建第二个实例，导致同一用户并发写丢事件。
                tenant["_refs"] = 1
                tenant["_evictable"] = False
                self._cache[uid] = tenant
                web_app.bootstrap_tenant(tenant)
                self._evict_if_needed()
                return tenant
            tenant["_refs"] += 1
            return tenant

    def _evict_if_needed(self) -> None:
        """只淘汰没有在途请求的租户，避免同一用户出现两个引擎实例写同一文件。"""
        while len(self._cache) > self.max_cached:
            victim = next((k for k, v in self._cache.items()
                           if v.get("_refs", 0) == 0), None)
            if victim is None:
                # 全部在用：宁可暂时超出缓存上限，也不制造双实例。
                break
            old = self._cache.pop(victim)
            old["_evictable"] = True
            with old["lock"]:
                try:
                    if old.get("system") is not None:
                        old["system"].flush()
                except Exception:  # noqa: BLE001
                    pass

    def release(self, tenant: dict) -> None:
        """请求结束时归还引用；被标记淘汰的租户在此刻 flush。"""
        with self._lock:
            tenant["_refs"] = max(0, int(tenant.get("_refs", 0)) - 1)
            if tenant.get("_evictable") and tenant["_refs"] == 0:
                with tenant["lock"]:
                    try:
                        if tenant.get("system") is not None:
                            tenant["system"].flush()
                    except Exception:  # noqa: BLE001
                        pass
                tenant["_evictable"] = False
            self._evict_if_needed()

    def invalidate(self, uid: str) -> None:
        uid = self._check(uid)
        with self._lock:
            old = self._cache.pop(uid, None)
            if old and old.get("system") is not None:
                old["system"].flush()

    def flush_all(self) -> None:
        with self._lock:
            for tenant in self._cache.values():
                if tenant.get("system") is not None:
                    tenant["system"].flush()

    def cached_count(self) -> int:
        with self._lock:
            return len(self._cache)
