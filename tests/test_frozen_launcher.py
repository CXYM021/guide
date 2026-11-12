# -*- coding: utf-8 -*-
"""验收：单文件 exe 启动器用到的配置路径、提示文案、端口顺延与图标结构。

运行：cd product && python -m unittest discover -s tests -p "test_*.py" -v
exe 本身的解压实跑见 PACKAGING.txt 第四节（由构建后的独立脚本执行）。
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

import build_exe  # noqa: E402
import frozen_entry  # noqa: E402
import server  # noqa: E402


class TestFrozenLauncher(unittest.TestCase):
    def test_prof_config_points_to_editable_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(json.dumps({
                "host": "0.0.0.0", "port": 9123, "invite_code": "abc123",
                "data_dir": str(Path(td) / "mydata"),
            }), encoding="utf-8")
            with patch.dict("os.environ", {"PROF_CONFIG": str(cfg_path)}, clear=True):
                cfg = server.load_config()
            self.assertEqual(cfg["host"], "0.0.0.0")
            self.assertEqual(cfg["port"], 9123)
            self.assertEqual(cfg["invite_code"], "abc123")
            self.assertEqual(str(cfg["data_dir"]), str(Path(td) / "mydata"))
            cfg_path.unlink()
            with patch.dict("os.environ", {"PROF_CONFIG": str(cfg_path)}, clear=True):
                with self.assertRaises(ValueError, msg="显式指定的配置缺失必须报错"):
                    server.load_config()

    def test_lan_hint_differs_between_source_and_exe(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIn("start_lan.bat", server.lan_enable_hint())
        with patch.dict("os.environ", {"PROF_EXE_MODE": "1"}, clear=True):
            hint = server.lan_enable_hint()
            self.assertIn("config.json", hint)
            self.assertNotIn("start_lan.bat", hint,
                             "exe 用户没有 bat 文件，提示不能指向它")

    def test_access_payload_exposes_hint(self):
        try:
            from test_multitenant import ProductHarness
        except ImportError:  # pragma: no cover
            from tests.test_multitenant import ProductHarness  # type: ignore
        with tempfile.TemporaryDirectory() as td:
            app = ProductHarness(Path(td))
            try:
                code, body, _ = app.req("/api/access")
                self.assertEqual(code, 200)
                self.assertTrue(body["enable_lan_hint"])
                self.assertIn("只监听", body["notes"][0])
            finally:
                app.close()

    def test_free_port_skips_occupied(self):
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        try:
            picked = frozen_entry.free_port("127.0.0.1", port)
            self.assertGreater(picked, port)
            self.assertLess(picked, port + 20)
        finally:
            occupied.close()

    def test_ensure_config_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            first = frozen_entry.ensure_config(path)
            self.assertTrue(path.is_file())
            self.assertEqual(first["port"], 8765)
            first["invite_code"] = "keepme"
            path.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
            second = frozen_entry.ensure_config(path)
            self.assertEqual(second["invite_code"], "keepme",
                             "用户改过的配置不能被覆盖")

    def test_ico_has_valid_png_payload(self):
        with tempfile.TemporaryDirectory() as td:
            ico = Path(td) / "app.ico"
            build_exe.make_ico(PRODUCT_DIR / "ui" / "icon-192.png", ico)
            data = ico.read_bytes()
            self.assertEqual(struct.unpack("<HHH", data[:6]), (0, 1, 1))
            w, h, colors, reserved, planes, bits, size, offset = \
                struct.unpack("<BBBBHHII", data[6:22])
            self.assertEqual((w, h, colors, reserved, planes, bits, offset),
                             (192, 192, 0, 0, 1, 32, 22))
            self.assertEqual(size, len(data) - 22)
            self.assertTrue(data[offset:].startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
