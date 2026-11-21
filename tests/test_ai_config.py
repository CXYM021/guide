# -*- coding: utf-8 -*-
"""标准来源配置（界面里配 AI）：优先级、落盘、掩码、人话报错、不配也能加技能。

运行：python -m unittest discover -s tests -p "test_*.py" -v
不联网：AI 端点与本机 mock 服务对话，401 场景也用本地 mock 扮演。
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
    from test_custom_skills import MockAIServer  # noqa: E402
except ImportError:  # pragma: no cover - 以包方式导入时走这里
    from tests.test_multitenant import ProductHarness  # type: ignore
    from tests.test_custom_skills import MockAIServer  # type: ignore

import profile_ai  # noqa: E402
from skill_forge import forge_skill_spec  # noqa: E402

METRICS = [{"key": "practice_minutes", "label": "练习时长", "unit": "分钟",
            "range_low": 0, "range_high": 120}]
SKILL = "钢琴演奏"
AI_ENV = ("PROF_AI_ENDPOINT", "PROF_AI_KEY", "PROF_AI_MODEL",
          "PROF_AI_ALLOW_PRIVATE_URLS")


def clean_env(**extra):
    """把所有 PROF_AI_* 先清空，避免本机真实环境变量污染断言。"""
    env = {name: "" for name in AI_ENV}
    env.update(extra)
    return patch.dict(os.environ, env)


class UnauthorizedHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        body = json.dumps({"error": {"message": "invalid api key"}}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UnauthorizedServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), UnauthorizedHandler)
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


class TestAiConfigStore(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_default_is_not_configured(self):
        with clean_env():
            cfg = profile_ai.load_ai_config(self.root)
        self.assertFalse(cfg["endpoint"] or cfg["key"] or cfg["model"])
        status = profile_ai.public_status(cfg)
        self.assertFalse(status["configured"])
        self.assertEqual(len(status["presets"]), 5)

    def test_save_load_and_mask(self):
        with clean_env():
            profile_ai.save_ai_config(
                self.root, preset="deepseek",
                endpoint="https://api.deepseek.com", model="deepseek-chat",
                key="sk-abcdefgh12345678")
            cfg = profile_ai.load_ai_config(self.root)
            status = profile_ai.public_status(cfg)
        self.assertTrue(status["configured"])
        self.assertEqual(status["source"], "local")
        self.assertEqual(status["model"], "deepseek-chat")
        self.assertEqual(status["masked_key"], "sk-…5678")
        self.assertEqual(status["endpoint_host"], "api.deepseek.com")
        self.assertTrue(self.status_path_exists())

    def status_path_exists(self) -> bool:
        return profile_ai.secret_path(self.root).is_file()

    def test_payload_never_contains_plaintext_key(self):
        with clean_env():
            profile_ai.save_ai_config(self.root, preset="openai",
                                      endpoint="https://api.openai.com/v1",
                                      model="gpt-4o-mini",
                                      key="sk-secret-value-9876")
            payload = profile_ai.config_payload(self.root)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("sk-secret-value-9876", blob)
        self.assertIn("…9876", blob)

    def test_blank_key_keeps_previous(self):
        with clean_env():
            profile_ai.save_ai_config(self.root, preset="custom",
                                      endpoint="https://a.example.com/v1",
                                      model="m1", key="sk-keep-me-0001")
            profile_ai.save_ai_config(self.root, preset="custom",
                                      endpoint="https://a.example.com/v1",
                                      model="m2", key="")
            cfg = profile_ai.load_ai_config(self.root)
        self.assertEqual(cfg["key"], "sk-keep-me-0001")
        self.assertEqual(cfg["model"], "m2")

    def test_clear_key_returns_to_local_template(self):
        with clean_env():
            profile_ai.save_ai_config(self.root, preset="custom",
                                      endpoint="https://a.example.com/v1",
                                      model="m1", key="sk-x-0002")
            cfg = profile_ai.clear_ai_key(self.root)
        self.assertFalse(profile_ai.public_status(cfg)["configured"])
        self.assertEqual(cfg["endpoint"], "https://a.example.com/v1")

    def test_bad_endpoint_is_rejected_in_plain_words(self):
        with clean_env():
            with self.assertRaises(profile_ai.AiConfigError) as ctx:
                profile_ai.save_ai_config(self.root, preset="custom",
                                          endpoint="api.deepseek.com",
                                          model="m", key="k")
        self.assertIn("http://", str(ctx.exception))

    def test_env_beats_file(self):
        with clean_env():
            profile_ai.save_ai_config(self.root, preset="custom",
                                      endpoint="https://file.example.com/v1",
                                      model="file-model", key="sk-file")
            with clean_env(PROF_AI_ENDPOINT="https://env.example.com/v1",
                           PROF_AI_KEY="sk-env",
                           PROF_AI_MODEL="env-model"):
                cfg = profile_ai.load_ai_config(self.root)
        self.assertEqual(cfg["source"], "env")
        self.assertEqual(cfg["endpoint"], "https://env.example.com/v1")
        self.assertEqual(cfg["model"], "env-model")
        self.assertFalse(profile_ai.public_status(cfg)["key_on_disk"])

    def test_partial_env_does_not_hijack_file_config(self):
        with clean_env(PROF_AI_MODEL="env-model"):
            profile_ai.save_ai_config(self.root, preset="custom",
                                      endpoint="https://file.example.com/v1",
                                      model="file-model", key="sk-file")
            with clean_env(PROF_AI_MODEL="env-model"):
                cfg = profile_ai.load_ai_config(self.root)
        self.assertTrue(profile_ai.public_status(cfg)["configured"])
        self.assertEqual(cfg["model"], "env-model")   # 单项覆盖生效
        self.assertEqual(cfg["endpoint"], "https://file.example.com/v1")


class TestForgeIntegration(unittest.TestCase):
    """配好的东西必须真能改变生成结果，不能只是界面上显示「已配置」。"""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_local_mode_never_touches_network(self):
        cfg = {"endpoint": "http://127.0.0.1:9/v1", "key": "k",
               "model": "m", "allow_private": True}
        spec = forge_skill_spec(name=SKILL, metrics=METRICS, now=1000,
                                config=cfg, mode="local")
        self.assertFalse(spec["provenance"]["ai_used"])
        self.assertIn("通用起步标准", spec["provenance"]["reason"])

    def test_file_config_drives_generation(self):
        mock = MockAIServer()
        try:
            with clean_env(PROF_AI_ALLOW_PRIVATE_URLS="1"):
                profile_ai.save_ai_config(
                    self.root, preset="custom", endpoint=mock.base + "/v1",
                    model="mock-model", key="sk-mock")
                cfg = profile_ai.load_ai_config(self.root)
                spec = forge_skill_spec(
                    name=SKILL, metrics=METRICS, source_urls=[mock.base + "/ref"],
                    now=1000, config=cfg, mode="auto")
        finally:
            mock.close()
        p = spec["provenance"]
        self.assertTrue(p["ai_used"], p)
        self.assertEqual(p["model"], "mock-model")
        self.assertEqual(len(p["sources"]), 1)


class TestAiConfigApi(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.td.name)
        self.app = ProductHarness(self.data_dir)
        _, _, self.cookie = self.app.register("alice")

    def tearDown(self) -> None:
        self.app.close()
        self.td.cleanup()

    def test_requires_login(self):
        for path, method in (("/api/ai/config", None),
                             ("/api/ai/config", {"endpoint": "https://x/v1"}),
                             ("/api/ai/test", {})):
            code, body, _ = self.app.req(path, method)
            self.assertEqual(code, 401, body)

    def test_save_then_read_without_leaking_key(self):
        with clean_env():
            code, body, _ = self.app.req("/api/ai/config", {
                "endpoint": "https://api.deepseek.com", "model": "deepseek-chat",
                "key": "sk-live-abcdef123456"}, cookie=self.cookie)
            self.assertEqual(code, 200, body)
            self.assertTrue(body["configured"])
            self.assertNotIn("sk-live-abcdef123456",
                             json.dumps(body, ensure_ascii=False))
            code, got, _ = self.app.req("/api/ai/config", cookie=self.cookie)
        self.assertEqual(code, 200, got)
        self.assertTrue(got["configured"])
        self.assertEqual(got["masked_key"], "sk-…3456")
        self.assertNotIn("sk-live-abcdef123456",
                         json.dumps(got, ensure_ascii=False))

    def test_clear_key_via_api(self):
        with clean_env():
            self.app.req("/api/ai/config", {
                "endpoint": "https://api.deepseek.com", "model": "deepseek-chat",
                "key": "sk-clear-1111"}, cookie=self.cookie)
            code, body, _ = self.app.req("/api/ai/config",
                                         {"clear_key": True}, cookie=self.cookie)
        self.assertEqual(code, 200, body)
        self.assertFalse(body["configured"])

    def test_payload_shows_presets_for_the_form(self):
        code, body, _ = self.app.req("/api/skills/custom", cookie=self.cookie)
        self.assertEqual(code, 200, body)
        ai = body["ai"]
        self.assertIn("presets", ai)
        labels = [p["label"] for p in ai["presets"]]
        self.assertIn("DeepSeek", labels)
        self.assertIn("我自己填", labels)

    def test_bad_endpoint_returns_plain_words(self):
        with clean_env():
            code, body, _ = self.app.req("/api/ai/config", {
                "endpoint": "api.deepseek.com", "model": "m", "key": "k"},
                cookie=self.cookie)
        self.assertEqual(code, 400, body)
        self.assertIn("http://", body["error"])

    def test_test_endpoint_translates_401(self):
        bad = UnauthorizedServer()
        try:
            with clean_env():
                code, body, _ = self.app.req("/api/ai/test", {
                    "endpoint": bad.base + "/v1", "model": "m",
                    "key": "sk-wrong"}, cookie=self.cookie)
        finally:
            bad.close()
        self.assertFalse(body["ok"], body)
        self.assertIn("钥匙无效", body["error"])

    def test_test_endpoint_success_message(self):
        mock = MockAIServer()
        try:
            with clean_env(PROF_AI_ALLOW_PRIVATE_URLS="1"):
                code, body, _ = self.app.req("/api/ai/test", {
                    "endpoint": mock.base + "/v1", "model": "mock-model",
                    "key": "sk-ok"}, cookie=self.cookie)
        finally:
            mock.close()
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertIn("毫秒", body.get("message", ""))

    def test_add_skill_local_mode_works_without_ai(self):
        with clean_env():
            code, body, _ = self.app.req("/api/skills/custom/add", {
                "name": SKILL, "metrics": METRICS, "source_urls": [],
                "mode": "local"}, cookie=self.cookie)
        self.assertEqual(code, 200, body)
        self.assertFalse(body["provenance"]["ai_used"])
        self.assertEqual(body["skills"][0]["skill_id"], SKILL)


if __name__ == "__main__":
    unittest.main()
