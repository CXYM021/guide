# -*- coding: utf-8 -*-
"""自定义技能增删、规则判定、隔离、持久化，以及 AI 联网生成的可验证链路。

运行：cd product && python -m unittest discover -s tests -p "test_*.py" -v
AI 部分用本机 mock HTTP 服务同时扮演「公开资料页」与「OpenAI 兼容端点」，
不依赖任何真实外部账号或网络，能确定性验证抓取→模型→校验→落盘全链路。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

try:
    from test_multitenant import ProductHarness  # noqa: E402
except ImportError:  # pragma: no cover - 以包方式导入时走这里
    from tests.test_multitenant import ProductHarness  # type: ignore

METRICS = [{"key": "practice_minutes", "label": "练习时长", "unit": "分钟",
            "range_low": 0, "range_high": 120}]
SKILL = "钢琴演奏"

GOOD_AI = {
    "metrics": [{"key": "practice_minutes", "min_valid": 10, "good": 45,
                 "level_requirements": [0, 10, 20, 30, 45, 60, 80, 100],
                 "note": "依据资料分级结构"}],
    "level_observables": [f"LV{i + 1}：可稳定完成对应级别练习" for i in range(8)],
    "description": "依据所提供分级资料自建的练习规则，非官方标准。",
}
BAD_AI = dict(GOOD_AI, metrics=[{
    "key": "practice_minutes", "min_valid": 99999, "good": 99999,
    "level_requirements": [0] * 8, "note": "越界值"}])


class MockAIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if not self.path.startswith("/ref"):
            return self.send_error(404)
        body = (b"<html><body><h1>Graded practice</h1><p>"
                b"Grade 1 to Grade 8; daily practice minutes increase by level."
                b"</p></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return self.send_error(400)
        corpus = json.dumps(payload.get("messages", []), ensure_ascii=False)
        # 让测试能断言「资料正文确实进了模型提示词」
        self.server.last_corpus_has_source = "Grade 1 to Grade 8" in corpus
        spec = BAD_AI if self.server.mode == "bad" else GOOD_AI
        content = json.dumps(spec, ensure_ascii=False)
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class MockAIServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockAIHandler)
        self.httpd.mode = "good"
        self.httpd.last_corpus_has_source = False
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class TestCustomSkills(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.td.name)
        self.app = ProductHarness(self.data_dir)
        _, _, self.cookie = self.app.register("alice")

    def tearDown(self) -> None:
        self.app.close()
        self.td.cleanup()

    def add(self, name=SKILL, metrics=None, urls=None):
        return self.app.req("/api/skills/custom/add", {
            "name": name, "metrics": metrics or METRICS,
            "source_urls": urls or [],
        }, cookie=self.cookie)

    def skill_ids(self):
        return [s["skill_id"] for s in
                self.app.req("/api/skills", cookie=self.cookie)[1]["skills"]]

    # ---------- 增删与判定 ----------
    def test_add_local_template_then_practice_then_remove(self):
        code, empty, _ = self.app.req("/api/skills/custom", cookie=self.cookie)
        self.assertEqual(code, 200)
        self.assertEqual(empty["skills"], [])
        self.assertFalse(empty["ai"]["configured"])

        code, added, _ = self.add()
        self.assertEqual(code, 200, added)
        self.assertFalse(added["provenance"]["ai_used"])
        self.assertEqual(added["provenance"]["mode"], "local")
        self.assertIn("未配置", added["provenance"]["reason"])
        self.assertIn(SKILL, self.skill_ids())

        rules = self.app.req("/api/rules", cookie=self.cookie)[1]
        self.assertIn(SKILL, rules["skill_rules"])
        self.assertIn(SKILL, rules["settlement_forms"])
        spec = rules["skill_rules"][SKILL]["metrics"][0]
        self.assertEqual(spec["key"], "practice_minutes")

        body = {"skill_id": SKILL, "difficulty": 3, "quality": 80,
                "feedback": "external", "task_key": "音阶练习",
                "metrics": {"practice_minutes": spec["range_high"]}}
        code, ok, _ = self.app.req("/api/practice", body, cookie=self.cookie)
        self.assertEqual(code, 200, ok)
        self.assertTrue(ok["verdict"]["valid"], ok["verdict"]["reason_code"])
        self.assertGreater(ok["verdict"]["gain"], 0)

        below = dict(body, task_key="未达标",
                     metrics={"practice_minutes": spec["range_low"]})
        code, bad, _ = self.app.req("/api/practice", below, cookie=self.cookie)
        self.assertEqual(code, 200)
        self.assertFalse(bad["verdict"]["valid"],
                         "低于 min_valid 必须被技能指标门拒绝")

        code, removed, _ = self.app.req(
            "/api/skills/custom/remove", {"skill_id": SKILL},
            cookie=self.cookie)
        self.assertEqual(code, 200, removed)
        self.assertNotIn(SKILL, self.skill_ids())
        health = self.app.req("/api/health", cookie=self.cookie)[1]
        self.assertEqual(health["verify"], [])
        self.assertEqual(health["custom_verify"], [])

    def test_builtin_names_are_protected(self):
        code, body, _ = self.add(name="英语口语")
        self.assertEqual(code, 400)
        self.assertIn("内置技能", body["error"])
        code, body, _ = self.app.req(
            "/api/skills/custom/remove", {"skill_id": "英语口语"},
            cookie=self.cookie)
        self.assertEqual(code, 400)
        self.assertIn("内置技能", body["error"])

    def test_invalid_metric_input_is_rejected(self):
        code, body, _ = self.add(metrics=[{
            "key": "X", "label": "x", "unit": "项",
            "range_low": 0, "range_high": 10}])
        self.assertEqual(code, 400)
        code, body, _ = self.add(metrics=[{
            "key": "practice_minutes", "label": "x", "unit": "项",
            "range_low": 10, "range_high": 5}])
        self.assertEqual(code, 400)

    def test_two_users_never_share_custom_rules(self):
        _, _, bob = self.app.register("bob")
        self.assertEqual(self.add()[0], 200)
        self.assertIn(SKILL, self.skill_ids())
        bob_ids = [s["skill_id"] for s in
                   self.app.req("/api/skills", cookie=bob)[1]["skills"]]
        self.assertNotIn(SKILL, bob_ids)
        bob_custom = self.app.req("/api/skills/custom", cookie=bob)[1]
        self.assertEqual(bob_custom["skills"], [])

    def test_restart_and_export_import_preserve_custom_skill(self):
        self.assertEqual(self.add()[0], 200)
        bundle = self.app.req("/api/export", cookie=self.cookie)[1]
        self.assertIn("custom_skills.json", bundle["files"])
        self.assertIsNotNone(bundle["files"]["custom_skills.json"])

        self.app.close()
        self.app = ProductHarness(self.data_dir)
        self.assertIn(SKILL, self.skill_ids())
        code, imported, _ = self.app.req(
            "/api/import", {"bundle": bundle}, cookie=self.cookie)
        self.assertEqual(code, 200, imported)
        self.assertIn(SKILL, self.skill_ids())

    def test_capture_stop_builds_form_for_custom_skill(self):
        """用户给自定义技能挂机后，收口必须也能出结算问卷。"""
        from capture_ledger import CaptureLedger
        from custom_skills import CustomSkillRegistry
        from skill_forge import forge_skill_spec

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reg = CustomSkillRegistry(root / "custom_skills.json")
            reg.add(forge_skill_spec(name=SKILL, metrics=METRICS, now=1000))
            parts = reg.form_for(SKILL)
            self.assertIsNotNone(parts)
            ledger = CaptureLedger(root / "captures.json", clock=lambda: 1000)
            started = ledger.start(SKILL, now=1000)
            row = ledger.stop(started["id"], [[600, 900]], last_event=None,
                              now=2800, level=1,
                              form_rule=parts[0], form_spec=parts[1])
            self.assertEqual(row["status"], "pending")
            keys = [q["key"] for q in row["form"]["questions"]]
            self.assertIn("practice_minutes", keys)
            self.assertEqual(row["form"]["question_count"], len(keys))

    # ---------- AI 联网生成（mock 端到端） ----------
    def test_ai_web_generation_is_real_and_auditable(self):
        mock = MockAIServer()
        try:
            env = {
                "PROF_AI_ENDPOINT": mock.base + "/v1",
                "PROF_AI_KEY": "test-key",
                "PROF_AI_MODEL": "mock-model",
                "PROF_AI_ALLOW_PRIVATE_URLS": "1",
            }
            with patch.dict(os.environ, env):
                code, added, _ = self.add(urls=[mock.base + "/ref"])
            self.assertEqual(code, 200, added)
            p = added["provenance"]
            self.assertTrue(p["ai_used"], p)
            self.assertEqual(p["mode"], "ai_web")
            self.assertEqual(p["model"], "mock-model")
            self.assertEqual(len(p["sources"]), 1)
            self.assertEqual(p["sources"][0]["url"], mock.base + "/ref")
            self.assertEqual(len(p["sources"][0]["sha256"]), 64)
            self.assertTrue(mock.httpd.last_corpus_has_source,
                            "抓取到的资料正文必须进入模型提示词")

            rules = self.app.req("/api/rules", cookie=self.cookie)[1]
            metric = rules["skill_rules"][SKILL]["metrics"][0]
            self.assertEqual(metric["min_valid"], GOOD_AI["metrics"][0]["min_valid"])
            self.assertEqual(metric["good"], GOOD_AI["metrics"][0]["good"])
            self.assertEqual(metrics_levels(rules), GOOD_AI["metrics"][0]["level_requirements"])
        finally:
            mock.close()

    def test_ai_output_out_of_range_falls_back_to_local(self):
        mock = MockAIServer()
        mock.httpd.mode = "bad"
        try:
            env = {
                "PROF_AI_ENDPOINT": mock.base + "/v1",
                "PROF_AI_KEY": "test-key", "PROF_AI_MODEL": "mock-model",
                "PROF_AI_ALLOW_PRIVATE_URLS": "1",
            }
            with patch.dict(os.environ, env):
                code, added, _ = self.add(name="越界回退",
                                          urls=[mock.base + "/ref"])
            self.assertEqual(code, 200, added)
            p = added["provenance"]
            self.assertFalse(p["ai_used"])
            self.assertEqual(p["mode"], "local")
            self.assertIn("未通过校验", p["reason"])
            rules = self.app.req("/api/rules", cookie=self.cookie)[1]
            self.assertLessEqual(
                rules["skill_rules"]["越界回退"]["metrics"][0]["min_valid"], 120)
        finally:
            mock.close()

    def test_private_url_blocked_without_opt_in(self):
        mock = MockAIServer()
        try:
            env = {
                "PROF_AI_ENDPOINT": mock.base + "/v1",
                "PROF_AI_KEY": "test-key", "PROF_AI_MODEL": "mock-model",
                "PROF_AI_ALLOW_PRIVATE_URLS": "0",
            }
            with patch.dict(os.environ, env):
                code, added, _ = self.add(name="内网拦截",
                                          urls=[mock.base + "/ref"])
            self.assertEqual(code, 200, added)
            self.assertFalse(added["provenance"]["ai_used"])
            self.assertIn("抓取失败", added["provenance"]["reason"])
        finally:
            mock.close()


def metrics_levels(rules: dict) -> list[int]:
    return list(rules["skill_rules"][SKILL]["metrics"][0]["level_requirements"])


if __name__ == "__main__":
    unittest.main()
