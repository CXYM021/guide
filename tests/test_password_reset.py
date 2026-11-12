# -*- coding: utf-8 -*-
"""密码重置：本机改密后旧会话必须失效，新旧密码行为要可预期。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT_DIR))

from auth import AuthError, AuthStore  # noqa: E402


class PasswordResetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "data"
        self.store = AuthStore(self.dir)
        self.store.register("Alice", "password123")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reset_switches_password_and_kills_old_sessions(self) -> None:
        token = self.store.login("alice", "password123")
        self.assertIsNotNone(self.store.user_for_token(token))

        self.store.set_password("alice", "newpassword456")

        self.assertIsNone(self.store.user_for_token(token), "旧会话必须失效")
        with self.assertRaises(AuthError):
            self.store.login("alice", "password123")
        self.assertTrue(self.store.login("ALICE", "newpassword456"))

    def test_reset_rejects_short_password_and_unknown_user(self) -> None:
        with self.assertRaises(AuthError):
            self.store.set_password("alice", "short")
        with self.assertRaises(AuthError):
            self.store.set_password("nobody", "password123")


if __name__ == "__main__":
    unittest.main()
