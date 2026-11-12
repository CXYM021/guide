# -*- coding: utf-8 -*-
"""登录凭据回归：多次登录、大小写/全角变体、旧库迁移、限流隔离。

运行：cd product && python -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

from auth import AuthError, AuthStore, username_key  # noqa: E402

OLD_DDL = """
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_login_at INTEGER
);
CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
"""


def _insert_old_user(conn, uid: str, name: str, password: str,
                     created_at: int) -> None:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 120_000).hex()
    conn.execute(
        "INSERT INTO users(id,username,password_hash,salt,created_at)"
        " VALUES(?,?,?,?,?)", (uid, name, digest, salt.hex(), created_at))


class TestLoginCredentials(unittest.TestCase):
    def test_same_credentials_work_repeatedly(self):
        with tempfile.TemporaryDirectory() as td:
            store = AuthStore(Path(td))
            store.register("alice", "password123")
            tokens = [store.login("alice", "password123") for _ in range(5)]
            self.assertEqual(len(set(tokens)), 5, "每次登录应发新会话")
            for token in tokens:
                self.assertEqual(store.user_for_token(token)["username"], "alice")
            store.logout(tokens[0])
            self.assertIsNone(store.user_for_token(tokens[0]))
            self.assertEqual(store.user_for_token(tokens[1])["username"], "alice")

    def test_case_and_width_variants_login(self):
        with tempfile.TemporaryDirectory() as td:
            store = AuthStore(Path(td))
            store.register("Alice", "password123")
            for variant in ("Alice", "alice", "ALICE", " Alice ",
                            "Ａｌｉｃｅ", "ＡＬＩＣＥ"):
                with self.subTest(variant=variant):
                    self.assertTrue(store.login(variant, "password123"))
            self.assertEqual(store.user_for_token(
                store.login("alice", "password123"))["username"], "Alice",
                "显示名必须保留注册时的写法")
            self.assertEqual(username_key("Ａｌｉｃｅ"), "alice")

    def test_case_variant_registration_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = AuthStore(Path(td))
            store.register("Alice", "password123")
            for variant in ("alice", "ALICE", "Ａｌｉｃｅ"):
                with self.subTest(variant=variant):
                    with self.assertRaises(AuthError):
                        store.register(variant, "anotherpass")
            self.assertEqual(store.user_count(), 1)

    def test_wrong_password_and_unknown_user(self):
        with tempfile.TemporaryDirectory() as td:
            store = AuthStore(Path(td))
            store.register("alice", "password123")
            for name, pwd in (("alice", "wrongpass"), ("nobody", "password123"),
                              ("alice", "Password123")):
                with self.subTest(name=name):
                    with self.assertRaises(AuthError):
                        store.login(name, pwd)

    def test_legacy_db_migration_keeps_both_accounts_loginable(self):
        """旧库里 Alice / alice 是两个不同账号，迁移后都不能被劫持。"""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "app.db"
            conn = sqlite3.connect(db)
            conn.executescript(OLD_DDL)
            _insert_old_user(conn, "u1", "Alice", "firstpass", 100)
            _insert_old_user(conn, "u2", "alice", "secondpass", 200)
            conn.commit()
            conn.close()

            store = AuthStore(Path(td))
            probe = sqlite3.connect(db)
            try:
                cols = {r[1] for r in probe.execute("PRAGMA table_info(users)")}
            finally:
                probe.close()
            self.assertIn("username_key", cols)
            self.assertEqual(store.user_count(), 2)
            self.assertEqual(store.user_for_token(
                store.login("Alice", "firstpass"))["username"], "Alice")
            self.assertEqual(store.user_for_token(
                store.login("alice", "secondpass"))["username"], "alice")
            # 大写变体回落到最早注册的账号，密码必须对得上
            self.assertTrue(store.login("ALICE", "firstpass"))
            with self.assertRaises(AuthError):
                store.login("ALICE", "secondpass")

    def test_rate_limit_is_per_account(self):
        with tempfile.TemporaryDirectory() as td:
            store = AuthStore(Path(td))
            store.register("alice", "password123")
            store.register("bob", "password123")
            for _ in range(20):
                with self.assertRaises(AuthError):
                    store.login("alice", "wrongpass", client_ip="10.0.0.1")
            with self.assertRaises(AuthError) as caught:
                store.login("alice", "password123", client_ip="10.0.0.1")
            self.assertIn("太频繁", str(caught.exception))
            self.assertTrue(store.login("bob", "password123",
                                        client_ip="10.0.0.1"),
                            "同一来源下别的用户不应被连带限流")

    def test_frontend_blocks_autocapitalize_and_resets_mode(self):
        shell = (PRODUCT_DIR / "ui" / "auth_shell.html").read_text(encoding="utf-8")
        self.assertIn('autocapitalize="none"', shell)
        self.assertIn("setMode('login')", shell)
        html = (PRODUCT_DIR / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('autocapitalize="none"', html,
                      "改动认证壳后需运行 python build_ui.py")


if __name__ == "__main__":
    unittest.main()
