# -*- coding: utf-8 -*-
"""页面外壳的版本一致性：升级后不能还让浏览器用旧页面。

两个失败一起防：
1. ui/index.html 里没有 __APP_VERSION__，页面就没法和服务器握手；
2. ui/sw.js 的缓存名不跟着版本走，旧壳会被 Service Worker 一直供着
   （1.0.3 升级后「读取失败：Cannot read properties of undefined (reading 'map')」
   就是这么来的：旧页面向新接口要 baseline，而新接口改成了 baseline_by_skill）。
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def product_version() -> str:
    text = (ROOT / "server.py").read_text(encoding="utf-8")
    hit = re.search(r'PRODUCT_VERSION\s*=\s*"([^"]+)"', text)
    assert hit is not None
    return hit.group(1)


class UiVersionTest(unittest.TestCase):
    def test_index_carries_app_version(self) -> None:
        version = product_version()
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'window.__APP_VERSION__ = "{version}"', html)

    def test_service_worker_cache_tracks_version(self) -> None:
        version = product_version()
        sw = (ROOT / "ui" / "sw.js").read_text(encoding="utf-8")
        self.assertIn(f"guide-shell-{version}", sw)
        others = set(re.findall(r"guide-shell-[0-9A-Za-z.\-]+", sw))
        self.assertEqual(others, {f"guide-shell-{version}"})

    def test_shell_is_network_first(self) -> None:
        sw = (ROOT / "ui" / "sw.js").read_text(encoding="utf-8")
        self.assertIn("req.mode === 'navigate'", sw)
        # 页面请求必须先走 fetch；先查缓存就是旧版本赖着不走的根因
        shell_block = sw.split("if (isShell) {", 1)[1].split("if (ASSETS", 1)[0]
        self.assertIn("fetch(req)", shell_block)
        self.assertIn(".catch(offlineFallback)", shell_block)
        self.assertNotIn("caches.match(req)", shell_block)

    def test_page_has_version_guard(self) -> None:
        page = (ROOT / "engine" / "web_ui.py").read_text(encoding="utf-8")
        self.assertIn("async function versionGuard()", page)
        self.assertIn("app-version-retry", page)
        self.assertIn("navigator.serviceWorker.getRegistrations()", page)


if __name__ == "__main__":
    unittest.main()
