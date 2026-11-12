# -*- coding: utf-8 -*-
"""「同一组账号密码第二次登录失败」的回归测试。

覆盖两条真实成因：
1. 用户名里混进不可见格式字符（U+200B/U+FEFF 等）——手机输入法、复制粘贴、
   密码管理器自动填充都会带出来，肉眼完全一样；
2. 旧库里的 username_key 由旧算法算出，重启后仍按旧键查找。

以及密码不做静默裁剪这一条边界：密码首尾空格必须原样参与哈希。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

from auth import AuthError, AuthStore, clean_username, username_key  # noqa: E402

try:
    from test_multitenant import ProductHarness
except ImportError:  # pragma: no cover
    from tests.test_multitenant import ProductHarness  # type: ignore


class TestUsernameKey(unittest.TestCase):
    def test_invisible_characters_are_ignored(self):
        want = username_key("Alice")
        for raw in ("Alic\u200be", "\ufeffAlice", "Ali\u200bce", "Alice\u2060",
                    " Alice ", "Ａｌｉｃｅ", "ALICE", "alice"):
            self.assertEqual(username_key(raw), want, f"{raw!r} 应折叠成同一个键")

    def test_clean_username_keeps_visible_text(self):
        self.assertEqual(clean_username("  \uff21lice\u200b  "), "Alice")
        self.assertEqual(clean_username("小明"), "小明")

    def test_password_is_not_normalised(self):
        """密码不能被 NFKC/去空格悄悄改掉，否则用户记的密码对不上。"""
        with tempfile.TemporaryDirectory() as td:
            store = AuthStore(Path(td))
            store.register("Bob", " pass1234")
            self.assertTrue(store.login("Bob", " pass1234"))
            with self.assertRaises(AuthError):
                store.login("Bob", "pass1234")


class TestLoginRepeatable(unittest.TestCase):
    def test_same_credentials_three_times(self):
        with tempfile.TemporaryDirectory() as td:
            store = AuthStore(Path(td))
            store.register("Alice", "password123")
            for i in range(3):
                self.assertTrue(store.login("Alice", "password123"),
                                f"第 {i + 1} 次登录失败")

    def test_variants_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            AuthStore(root).register("Alice", "password123")
            reopened = AuthStore(root)   # 模拟关掉进程再打开
            for name in ("Alice", "alice", "ALICE", "  Alice  ",
                         "\uff21lice", "Alic\u200be"):
                self.assertTrue(reopened.login(name, "password123"),
                                f"重启后用 {name!r} 登录失败")

    def test_legacy_key_with_invisible_char_is_repaired(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            AuthStore(root).register("Alice", "password123")
            db = root / "app.db"
            # 注意：`with sqlite3.connect(...)` 只提交事务、不关闭连接；
            # 在 Windows 上留着句柄会让退出的临时目录删不掉（WinError 32）。
            conn = sqlite3.connect(db)
            try:
                conn.execute("UPDATE users SET username_key=?",
                             ("alic\u200be",))
                conn.commit()
            finally:
                conn.close()
            reopened = AuthStore(root)        # 启动时重建旧键
            self.assertTrue(reopened.login("Alice", "password123"))

    def test_register_normalises_display_name(self):
        with tempfile.TemporaryDirectory() as td:
            store = AuthStore(Path(td))
            user = store.register("\uff21lice\u200b", "password123")
            self.assertEqual(user["username"], "Alice")


class TestLoginOverHttp(unittest.TestCase):
    """真实 HTTP 路径：注册后连续登录三次，每次都要 200 并下发新 Cookie。"""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.app = ProductHarness(Path(self.td.name))

    def tearDown(self) -> None:
        self.app.close()
        self.td.cleanup()

    def post(self, path: str, body: dict):
        code, res, headers = self.app.req(path, body)
        return code, res, headers

    def test_login_three_times_over_http(self):
        code, _, _ = self.post("/api/auth/register",
                               {"username": "Alice", "password": "password123"})
        self.assertEqual(code, 201)
        for i in range(3):
            code, res, headers = self.post(
                "/api/auth/login",
                {"username": "Alice", "password": "password123"})
            self.assertEqual(code, 200, f"第 {i + 1} 次登录失败：{res}")
            self.assertIn("sid=", headers.get("Set-Cookie", ""),
                          f"第 {i + 1} 次登录没有下发会话 Cookie")

    def test_wrong_password_still_rejected(self):
        self.post("/api/auth/register",
                  {"username": "Alice", "password": "password123"})
        code, res, _ = self.post("/api/auth/login",
                                 {"username": "Alice", "password": "wrongpass1"})
        self.assertEqual(code, 400)
        self.assertEqual(res.get("error"), "用户名或密码不正确")


if __name__ == "__main__":
    unittest.main()
