# -*- coding: utf-8 -*-
"""验收：他人电脑 / 手机能否真的连上，以及 PWA 静态资源是否可用。

运行：cd product && python -m unittest discover -s tests -p "test_*.py" -v
其中 test_real_lan_ip_is_reachable 会真实绑定 0.0.0.0，并用本机网卡 IPv4
发 HTTP 请求，等价于「另一台设备连进来」的服务端路径。
"""

from __future__ import annotations

import ipaddress
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

import build_assets  # noqa: E402
import build_ui  # noqa: E402
import server as product_server  # noqa: E402
from server import STATIC_ASSETS  # noqa: E402

try:
    from test_multitenant import ProductHarness
except ImportError:  # pragma: no cover - 以包方式导入时走这里
    from tests.test_multitenant import ProductHarness  # type: ignore


def raw_get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class TestRemoteAccess(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.td.name)
        self.app = ProductHarness(self.data_dir)

    def tearDown(self) -> None:
        self.app.close()
        self.td.cleanup()

    # ---------- PWA 静态资源 ----------
    def test_static_assets_are_anonymous(self):
        """浏览器拉 manifest/图标时不带 Cookie，绝不能被 401 拦下。"""
        base = f"http://127.0.0.1:{self.app.port}"
        for path, (_, ctype) in STATIC_ASSETS.items():
            code, data, headers = raw_get(base + path)
            self.assertEqual(code, 200, f"{path} 应匿名可访问")
            self.assertTrue(data, f"{path} 不能为空")
            self.assertIn(ctype.split(";")[0],
                         headers.get("Content-Type", ""),
                         f"{path} 的 Content-Type 不对")
        code, _, headers = raw_get(base + "/sw.js")
        self.assertEqual(code, 200)
        self.assertIn("no-cache", headers.get("Cache-Control", ""))
        code, _, _ = raw_get(base + "/manifest.webmanifest")
        self.assertEqual(code, 200)
        # 不存在的路径仍走认证，不能被静态白名单放行
        self.assertEqual(self.app.req("/api/skills")[0], 401)

    def test_png_icons_are_valid(self):
        for size in (192, 512):
            data = (PRODUCT_DIR / "ui" / f"icon-{size}.png").read_bytes()
            info = build_assets.verify_png(data, size)
            self.assertEqual(info["width"], size)
            self.assertEqual(info["height"], size)
            self.assertEqual(info["channels"], 4)
            self.assertEqual(info["chunks"], ["IDAT", "IEND", "IHDR"])
        # 篡改一个字节必须被 CRC 校验抓到
        bad = bytearray((PRODUCT_DIR / "ui" / "icon-192.png").read_bytes())
        bad[20] ^= 0xFF
        with self.assertRaises(ValueError):
            build_assets.verify_png(bytes(bad), 192)

    def test_manifest_and_sw_do_not_cache_user_data(self):
        manifest = json.loads(
            (PRODUCT_DIR / "ui" / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(len(manifest["icons"]), 2)
        self.assertTrue(all(i["type"] == "image/png" for i in manifest["icons"]))
        sw = (PRODUCT_DIR / "ui" / "sw.js").read_text(encoding="utf-8")
        self.assertIn("url.pathname.indexOf('/api/') === 0", sw)
        self.assertIn("return; // 一律走网络，不进缓存", sw,
                      "Service Worker 必须显式排除 /api/，否则多账号会串数据")
        self.assertIn("'/healthz'", sw)

    def test_ui_has_pwa_head_and_access_entry(self):
        html = (PRODUCT_DIR / "ui" / "index.html").read_text(encoding="utf-8")
        for needle in ('rel="manifest"', 'apple-touch-icon',
                       'name="theme-color"', 'id="access-mask"',
                       'access-btn', 'serviceWorker.register',
                       'navigator.clipboard'):
            self.assertIn(needle, html, f"前端缺少 {needle}")
        self.assertEqual(build_ui.build(), html,
                         "改了模板/认证壳后需运行 python build_ui.py")

    # ---------- 访问地址接口 ----------
    def test_access_default_is_localhost_only(self):
        code, body, _ = self.app.req("/api/access")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["listening_on_all"])
        self.assertFalse(body["lan_reachable_now"])
        self.assertEqual(body["lan_urls"], [],
                         "只监听 127.0.0.1 时不能给出连不上的假地址")
        self.assertTrue(any("只监听" in n for n in body["notes"]))

    def test_access_lan_ips_come_from_real_interfaces(self):
        self.app.close()
        self.app = ProductHarness(self.data_dir, host="0.0.0.0")
        code, body, _ = self.app.req("/api/access")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["listening_on_all"])
        self.assertTrue(body["lan_reachable_now"])
        self.assertTrue(body["lan_urls"])
        real = set(product_server.lan_addresses())
        for url in body["lan_urls"]:
            host = url.split("//")[1].split(":")[0]
            ip = ipaddress.IPv4Address(host)
            self.assertFalse(ip.is_loopback, url)
            self.assertIn(host, real, "地址必须来自真实网卡查询")

    def test_real_lan_ip_is_reachable(self):
        """真实绑定 0.0.0.0，用本机局域网 IPv4 请求（模拟他人设备）。"""
        self.app.close()
        self.app = ProductHarness(self.data_dir, host="0.0.0.0")
        ips = product_server.lan_addresses()
        if not ips:
            self.skipTest("本机没有可用于局域网的 IPv4，无法模拟他人设备")
        base = f"http://{ips[0]}:{self.app.port}"
        code, _, _ = raw_get(base + "/healthz")
        self.assertEqual(code, 200, "局域网 IP 上的健康检查必须可用")
        code, data, headers = raw_get(base + "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"auth-mask", data, "他人设备应拿到登录页")
        code, data, _ = raw_get(base + "/manifest.webmanifest")
        self.assertEqual(code, 200)
        self.assertIn(b"standalone", data)
        # 他人设备未登录仍不能读业务数据
        code, payload, _ = raw_get(base + "/api/skills")
        self.assertEqual(code, 401)
        self.assertFalse(json.loads(payload)["ok"])

    def test_public_mode_hides_internal_addresses(self):
        """反代 HTTPS 下不泄露内网拓扑，且 Cookie 带 Secure。"""
        self.app.close()
        self.app = ProductHarness(
            self.data_dir, host="0.0.0.0", secure_cookies=True,
            allowed_origins=["https://skill.example.com"],
            invite_code="letmein")
        base = f"http://127.0.0.1:{self.app.port}"
        code, body, _ = raw_get(base + "/api/access", {
            "Host": "skill.example.com",
            "X-Forwarded-Proto": "https",
        })
        self.assertEqual(code, 200)
        payload = json.loads(body)
        self.assertTrue(payload["public_mode"])
        self.assertTrue(payload["pwa_installable"])
        self.assertTrue(payload["invite_required"])
        self.assertTrue(payload["secure_cookies"])
        self.assertEqual(payload["lan_urls"], [],
                         "公网模式必须隐藏内网地址")

        req = urllib.request.Request(
            base + "/api/auth/register",
            data=json.dumps({"username": "remoteuser",
                             "password": "password123",
                             "invite_code": "letmein"}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Origin": "https://skill.example.com",
                     "Host": "skill.example.com",
                     "X-Forwarded-Proto": "https"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            self.assertEqual(resp.status, 201)
            self.assertIn("Secure", resp.headers.get("Set-Cookie", ""),
                          "HTTPS 部署下会话 Cookie 必须带 Secure")


if __name__ == "__main__":
    unittest.main()
