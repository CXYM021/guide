"""每日备份必须覆盖「进程不重启」的情况。

原来的实现只在租户第一次载入时备份一次；如果服务长驻、用户每天都来，
备份就会永远停在第一天。现在每个请求都会检查是否跨天，跨天补做一份。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:  # discover -s tests 时是顶层模块；-m unittest tests.xxx 时走包路径
    from test_multitenant import ProductHarness
except ImportError:  # pragma: no cover
    from tests.test_multitenant import ProductHarness  # type: ignore


class TestDailyBackupOnNewDay(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.td.name)
        self.app = ProductHarness(self.data_dir)
        code, body, cookie = self.app.register("backup_user")
        self.assertEqual(code, 201, body)
        self.cookie = cookie
        code, body, _ = self.app.req("/api/auth/me", cookie=self.cookie)
        self.assertEqual(code, 200, body)
        self.uid = body["user"]["id"]
        self.backups = self.data_dir / "users" / self.uid / "backups"

    def tearDown(self) -> None:
        self.app.close()
        self.td.cleanup()

    def _backup_days(self) -> list[str]:
        return sorted(p.name[len("daily-"):][:8]
                      for p in self.backups.glob("daily-*.zip"))

    def test_backup_is_taken_again_on_a_new_day(self) -> None:
        code, body, _ = self.app.req("/api/skills", cookie=self.cookie)
        self.assertEqual(code, 200, body)
        first = self._backup_days()
        self.assertEqual(len(first), 1, first)

        # 同一天再请求，不应该重复备份。
        for _ in range(3):
            self.app.req("/api/skills", cookie=self.cookie)
        self.assertEqual(self._backup_days(), first)

        # 打开开发者工具把模拟时钟推一天，下一次请求必须补一份新备份。
        code, body, _ = self.app.req("/api/settings", {"dev_tools": True},
                                     cookie=self.cookie)
        self.assertEqual(code, 200, body)
        code, body, _ = self.app.req("/api/advance", {"days": 1},
                                     cookie=self.cookie)
        self.assertEqual(code, 200, body)
        self.app.req("/api/skills", cookie=self.cookie)

        days = self._backup_days()
        self.assertEqual(len(days), 2, days)
        self.assertNotEqual(days[0], days[1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
