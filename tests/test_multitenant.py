# -*- coding: utf-8 -*-
"""产品验收：真实 HTTP、多用户隔离、并发、持久化、规则一致性。

运行：cd product && python -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

from server import DEFAULT_CONFIG, build_server  # noqa: E402

METRICS = {"continuous_minutes": 12, "active_phrases": 8,
           "listener_score": 85}


def practice_body(task: str) -> dict:
    return {"skill_id": "英语口语", "difficulty": 3, "quality": 80,
            "feedback": "external", "task_key": task, "metrics": dict(METRICS)}


class ProductHarness:
    def __init__(self, data_dir: Path, **overrides) -> None:
        cfg = dict(DEFAULT_CONFIG)
        cfg["data_dir"] = str(data_dir)
        cfg["host"] = "127.0.0.1"
        cfg["port"] = 0
        cfg.update(overrides)
        self.server = build_server(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.tenants.flush_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def req(self, path: str, body=None, cookie: str | None = None,
            origin: str | None = None):
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}, resp.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return exc.code, payload, exc.headers

    @staticmethod
    def cookie_of(headers) -> str:
        return str(headers.get("Set-Cookie", "")).split(";")[0]

    def register(self, name: str, password: str = "password123"):
        code, body, headers = self.req(
            "/api/auth/register",
            {"username": name, "password": password, "invite_code": ""})
        return code, body, self.cookie_of(headers)


class TestMultiUserProduct(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.td.name)
        self.app = ProductHarness(self.data_dir)

    def tearDown(self) -> None:
        self.app.close()
        self.td.cleanup()

    # ---------- 认证 ----------
    def test_auth_flow(self):
        code, body, _ = self.app.req("/api/skills")
        self.assertEqual(code, 401)
        self.assertFalse(body.get("ok"))

        code, _, _ = self.app.req("/api/auth/register",
                                  {"username": "a", "password": "password123"})
        self.assertEqual(code, 400, "用户名过短必须被拒")

        code, body, cookie = self.app.register("alice")
        self.assertEqual(code, 201, body)
        self.assertTrue(cookie.startswith("sid="))
        self.assertIn("id", body["user"])

        code, body, _ = self.app.req("/api/auth/me", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual(body["user"]["username"], "alice")

        code, body, _ = self.app.req("/api/skills", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual(len(body["skills"]), 7)

        code, _, _ = self.app.req("/api/auth/login",
                                  {"username": "alice", "password": "wrongpass"})
        self.assertEqual(code, 400)

        code, _, _ = self.app.req("/api/auth/logout", {}, cookie=cookie)
        self.assertEqual(code, 200)
        code, _, _ = self.app.req("/api/skills", cookie=cookie)
        self.assertEqual(code, 401, "登出后会话必须失效")

    def test_duplicate_username_rejected(self):
        self.assertEqual(self.app.register("bob")[0], 201)
        code, body, _ = self.app.register("bob")
        self.assertEqual(code, 400)
        self.assertIn("已被注册", body["error"])

    # ---------- 隔离 ----------
    def test_two_users_are_fully_isolated(self):
        _, _, a = self.app.register("alice")
        _, _, b = self.app.register("bob")
        self.assertNotEqual(a, b)

        code, res, _ = self.app.req("/api/practice", practice_body("A-1"),
                                    cookie=a)
        self.assertEqual(code, 200, res)
        self.assertTrue(res["verdict"]["valid"])

        pa = self.app.req("/api/skills", cookie=a)[1]
        pb = self.app.req("/api/skills", cookie=b)[1]
        xa = next(s["effective_exp"] for s in pa["skills"]
                  if s["skill_id"] == "英语口语")
        xb = next(s["effective_exp"] for s in pb["skills"]
                  if s["skill_id"] == "英语口语")
        self.assertGreater(xa, 0)
        self.assertEqual(xb, 0, "B 不能看到 A 的经验")
        self.assertEqual(len(pb["events"]), 0, "B 不能看到 A 的事件")
        self.assertEqual(pb["achievement_context"]["sessions"], 0)

        # 基准卡也按用户隔离
        seq = res["practice_event_seq"]
        # 改成客观度量事件才能设基准：用新任务再练一次
        body = practice_body("A-基准")
        body["feedback"] = "measured"
        code, res2, _ = self.app.req("/api/practice", body, cookie=a)
        self.assertEqual(code, 200)
        seq2 = res2["practice_event_seq"]
        code, created, _ = self.app.req(
            "/api/benchmark/create", {"event_seq": seq2}, cookie=a)
        self.assertEqual(code, 200, created)
        self.assertEqual(len(created["benchmark"]["active_cards"]), 1)
        pb2 = self.app.req("/api/skills", cookie=b)[1]
        self.assertEqual(len(pb2["benchmark"]["active_cards"]), 0)

    def test_export_import_is_scoped_to_current_user(self):
        _, _, a = self.app.register("alice")
        _, _, b = self.app.register("bob")
        self.app.req("/api/practice", practice_body("A-1"), cookie=a)
        self.app.req("/api/practice", practice_body("B-1"), cookie=b)
        bundle = self.app.req("/api/export", cookie=a)[1]
        self.assertEqual(bundle["files"]["skills.json"]["states"]
                         ["英语口语"]["total_exp"] > 0, True)
        before_b = self.app.req("/api/skills", cookie=b)[1]
        code, imported, _ = self.app.req(
            "/api/import", {"bundle": bundle}, cookie=a)
        self.assertEqual(code, 200, imported)
        self.assertTrue(imported["import_result"]["backup_file"].startswith(
            "before-import-"))
        after_b = self.app.req("/api/skills", cookie=b)[1]
        self.assertEqual(after_b["achievement_context"]["total_xp"],
                         before_b["achievement_context"]["total_xp"],
                         "导入 A 的备份不能改动 B 的数据")

    # ---------- 并发 ----------
    def test_concurrent_writes_keep_ledger_consistent(self):
        _, _, a = self.app.register("alice")
        _, _, b = self.app.register("bob")

        def post(cookie, prefix, i):
            return self.app.req("/api/practice",
                                practice_body(f"{prefix}-{i}"),
                                cookie=cookie)[0]

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = []
            for i in range(8):
                futures.append(pool.submit(post, a, "A", i))
                futures.append(pool.submit(post, b, "B", i))
            codes = [f.result() for f in futures]
        self.assertEqual(codes, [200] * 16)

        for cookie, label in ((a, "A"), (b, "B")):
            code, payload, _ = self.app.req("/api/skills", cookie=cookie)
            self.assertEqual(code, 200)
            self.assertEqual(payload["verify"], [])
            self.assertEqual(payload["capture"]["verify"], [])
            self.assertEqual(payload["benchmark"]["verify"], [])
            events = payload["events"]
            self.assertEqual(len(events), 8, f"{label} 应有 8 条事件")
            seqs = sorted(int(e["seq"]) for e in events)
            self.assertEqual(seqs, list(range(1, 9)),
                             f"{label} 事件序号必须连续")
            self.assertEqual(payload["achievement_context"]["sessions"], 8)
        health = self.app.req("/healthz")[1]
        self.assertEqual(health["users"], 2)

    def test_lru_eviction_never_creates_two_live_instances(self):
        """缓存上限 1、两用户交替并发：淘汰只允许发生在无在途请求时。"""
        self.app.close()
        self.app = ProductHarness(self.data_dir, max_cached_tenants=1)
        _, _, a = self.app.register("alice")
        _, _, b = self.app.register("bob")

        def post(cookie, prefix, i):
            return self.app.req("/api/practice",
                                practice_body(f"{prefix}-{i}"),
                                cookie=cookie)[0]

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = []
            for i in range(6):
                futures.append(pool.submit(post, a, "A", i))
                futures.append(pool.submit(post, b, "B", i))
            self.assertEqual([f.result() for f in futures], [200] * 12)
        for cookie in (a, b):
            payload = self.app.req("/api/skills", cookie=cookie)[1]
            self.assertEqual(payload["verify"], [])
            self.assertEqual(len(payload["events"]), 6)
            self.assertEqual(sorted(int(e["seq"]) for e in payload["events"]),
                             list(range(1, 7)))

    # ---------- 持久化 ----------
    def test_restart_preserves_each_users_data(self):
        _, _, a = self.app.register("alice")
        _, _, b = self.app.register("bob")
        self.app.req("/api/practice", practice_body("A-1"), cookie=a)
        self.app.req("/api/practice", practice_body("B-1"), cookie=a)
        self.app.req("/api/practice", practice_body("B-1"), cookie=b)

        def total(cookie):
            p = self.app.req("/api/skills", cookie=cookie)[1]
            return p["achievement_context"]["total_xp"]

        a_before, b_before = total(a), total(b)
        self.assertGreater(a_before, b_before)
        self.app.close()

        self.app = ProductHarness(self.data_dir)
        self.assertEqual(self.app.req("/api/auth/me", cookie=a)[0], 200,
                         "会话应跨重启保留")
        self.assertEqual(total(a), a_before)
        self.assertEqual(total(b), b_before)
        self.assertEqual(self.app.req("/api/health", cookie=a)[1]["verify"], [])

    # ---------- 服务与边界 ----------
    def test_public_health_ui_and_csrf(self):
        code, body, _ = self.app.req("/healthz")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["users"], 0)
        _, _, cookie = self.app.register("alice")
        code, _, _ = self.app.req("/api/practice", practice_body("x"),
                                  cookie=cookie, origin="http://evil.example")
        self.assertEqual(code, 403, "跨站 Origin 必须被拒")
        code, _, _ = self.app.req("/api/practice", practice_body("x"),
                                  cookie=cookie,
                                  origin=f"http://127.0.0.1:{self.app.port}")
        self.assertEqual(code, 200, "同域 Origin 必须放行")
        ui = (PRODUCT_DIR / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("auth-mask", ui)
        self.assertIn("window.__PRODUCT_AUTH__", ui)

    def test_ui_is_reproducible_from_product_sources(self):
        """前端必须从 product/ 内自包含模板重建，不能依赖开发工作区。"""
        import sys
        sys.path.insert(0, str(PRODUCT_DIR))
        import build_ui
        current = (PRODUCT_DIR / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(build_ui.build(), current,
                         "改动前端或认证壳后需运行 python build_ui.py 重建")
        sys.path.insert(0, str(PRODUCT_DIR / "engine"))
        from web_ui import HTML_PAGE
        import re
        main_script = re.findall(r"<script>(.*?)</script>", HTML_PAGE, re.S)[0]
        # 只替换末尾的自动启动调用，与 build_ui.py 的注入规则一致；
        # 页面里还有多处 `await refresh();`，不能被误改。
        guarded = main_script.replace(
            "\nrefresh();\n",
            "\nif (!window.__PRODUCT_AUTH__) { refresh(); }\n", 1)
        self.assertIn(guarded, current,
                      "SPA 主脚本必须与产品引擎内的模板同源")

    def test_practice_gain_is_stable(self):
        """同口径练习的增益锁定在 25 点：规则一改，这里必须先改并说明理由。

        旧版还逐字节比对开发树里的 realistic_rules.py；本仓库就是产品本身，
        没有「开发版」可对，因此只保留行为契约这一半。
        """
        _, _, cookie = self.app.register("alice")
        body = practice_body("parity-1")
        code, res, _ = self.app.req("/api/practice", body, cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(res["verdict"]["valid"])
        self.assertEqual(res["verdict"]["gain"], 25,
                         "难度3/质量80/他人点评/指标达标应得 25 点（同开发版仿真）")


if __name__ == "__main__":
    unittest.main()
