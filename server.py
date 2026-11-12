# -*- coding: utf-8 -*-
"""熟练度系统 · 独立可部署的多用户网站服务端。

启动：python server.py                 （读取同目录 config.json / config.example.json）
      PROF_PORT=9000 python server.py （环境变量覆盖）

只用 Python 标准库，Python >= 3.10。所有熟练度判定复用 engine/ 下的同一套
引擎文件，本文件只负责：账号会话、按会话绑定租户、静态页、请求分发。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import traceback
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ENGINE_DIR = HERE / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import console  # noqa: E402,F401 - 导入即把控制台切成 UTF-8
import web_app  # noqa: E402
from proficiency import SCHEMA_VERSION  # noqa: E402
from auth import AuthError, AuthStore  # noqa: E402
from tenant import TenantManager  # noqa: E402

PRODUCT_NAME = "proficiency-web"
PRODUCT_VERSION = "1.0.1"

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8765,
    "data_dir": "./data",
    "session_days": 30,
    "default_tz_offset_seconds": 28800,
    "max_users": 0,
    "invite_code": "",
    "secure_cookies": False,
    "max_cached_tenants": 64,
    "allowed_origins": [],
}

# 手机「添加到主屏」所需的静态资源。必须在登录检查之前返回：
# 浏览器拉取 manifest / 图标时不会带会话 Cookie，走认证会被 401。
# 用固定白名单而非目录映射，杜绝路径穿越。
STATIC_ASSETS: dict[str, tuple[str, str]] = {
    "/manifest.webmanifest": ("manifest.webmanifest",
                              "application/manifest+json; charset=utf-8"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    # 冻结成单文件 exe 后，内置配置在临时解压目录里、用户改不到。
    # PROF_CONFIG 让 exe 同目录的可编辑 config.json 成为配置来源。
    # 源码版不设该变量时行为与原来逐位一致。
    override = os.environ.get("PROF_CONFIG")
    if override:
        path = Path(override)
        if not path.is_file():
            raise ValueError(f"PROF_CONFIG 指向的配置文件不存在: {path}")
    else:
        path = HERE / "config.json"
        if not path.exists():
            path = HERE / "config.example.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("配置文件根节点必须是对象")
        cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    env_map = {
        "PROF_HOST": ("host", str), "PROF_PORT": ("port", int),
        "PROF_DATA_DIR": ("data_dir", str),
        "PROF_SESSION_DAYS": ("session_days", int),
        "PROF_TZ_OFFSET": ("default_tz_offset_seconds", int),
        "PROF_MAX_USERS": ("max_users", int),
        "PROF_INVITE_CODE": ("invite_code", str),
        "PROF_MAX_CACHED_TENANTS": ("max_cached_tenants", int),
    }
    for env, (key, cast) in env_map.items():
        if env in os.environ:
            cfg[key] = cast(os.environ[env])
    if "PROF_SECURE_COOKIES" in os.environ:
        cfg["secure_cookies"] = os.environ["PROF_SECURE_COOKIES"].lower() \
            in ("1", "true", "yes")
    if "PROF_ALLOWED_ORIGINS" in os.environ:
        cfg["allowed_origins"] = [
            x.strip() for x in os.environ["PROF_ALLOWED_ORIGINS"].split(",")
            if x.strip()]
    p = Path(cfg["data_dir"])
    cfg["data_dir"] = p if p.is_absolute() else (HERE / p).resolve()
    cfg["port"] = int(cfg["port"])
    cfg["session_days"] = max(1, int(cfg["session_days"]))
    return cfg


def lan_enable_hint() -> str:
    """「怎样改成对外监听」的提示按运行形态区分：exe 改 config.json，源码用脚本。"""
    if os.environ.get("PROF_EXE_MODE"):
        return ("把 config.json 里的 host 改成 0.0.0.0、设置 invite_code，"
                "关闭窗口后重新双击 exe；重启后会打印局域网地址。")
    return ("用 start_lan.bat / start_lan.sh 启动，"
            "或设置 PROF_HOST=0.0.0.0 后重启。")


def cookie_value(header: str | None, name: str) -> str | None:
    if not header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(header)
        morsel = jar.get(name)
        return morsel.value if morsel else None
    except Exception:  # noqa: BLE001 - Cookie 头畸形时按未登录处理
        return None


class ProductHandler(web_app.Handler):
    """在内核 Handler 外面加一层：静态页、认证、租户绑定、CSRF 检查。"""

    server_version = f"{PRODUCT_NAME}/{PRODUCT_VERSION}"

    def _send(self, code: int, payload, ctype: str = "application/json; charset=utf-8",
              cache: str = "no-store") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in getattr(self, "_extra_headers", {}).items():
            self.send_header(key, value)
        self._extra_headers = {}
        self.end_headers()
        try:
            if data:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            # 手机切后台、锁屏、刷新太快都会提前断开；不往控制台刷堆栈。
            return None

    def _cookie(self, token: str | None, max_age: int | None) -> None:
        parts = [f"sid={token or ''}", "HttpOnly", "SameSite=Lax", "Path=/"]
        if self.server.config.get("secure_cookies"):
            parts.append("Secure")
        parts.append(f"Max-Age={max_age if max_age is not None else 0}")
        self._extra_headers = {"Set-Cookie": "; ".join(parts)}

    def _current_user(self) -> dict | None:
        token = cookie_value(self.headers.get("Cookie"), "sid")
        return self.server.auth.user_for_token(token)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # 非浏览器客户端（curl / 健康检查）不带 Origin
        if origin in (self.server.config.get("allowed_origins") or []):
            return True
        parsed = urlparse(origin)
        return parsed.netloc == self.headers.get("Host", "") \
            and parsed.scheme in ("http", "https")

    # ---------- 路由 ----------
    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html") and method in ("GET", "HEAD"):
            return self._serve_ui()
        if path in STATIC_ASSETS and method in ("GET", "HEAD"):
            return self._serve_static(path, method == "HEAD")
        if path == "/favicon.ico":
            return self._send(204, b"")
        if path == "/healthz" and method == "GET":
            return self._send(200, {
                "ok": True, "product": PRODUCT_NAME,
                "version": PRODUCT_VERSION,
                "engine_schema": SCHEMA_VERSION,
                "users": self.server.auth.user_count(),
                "cached_tenants": self.server.tenants.cached_count(),
            })
        if path == "/api/access" and method == "GET":
            return self._send(200, self._access_payload())
        if path.startswith("/api/auth/"):
            return self._auth(method, path)
        if method == "POST" and not self._origin_ok():
            return self._send(403, {"ok": False, "error": "跨站请求被拒绝"})
        user = self._current_user()
        if user is None:
            return self._send(401, {"ok": False, "error": "未登录或会话已过期"})
        tenant = self.server.tenants.get(user["id"])
        token = web_app.bind_tenant(tenant)
        try:
            return super()._handle(method)
        finally:
            web_app.reset_tenant(token)
            self.server.tenants.release(tenant)

    def _serve_static(self, path: str, head_only: bool = False) -> None:
        filename, ctype = STATIC_ASSETS[path]
        asset = HERE / "ui" / filename
        if not asset.is_file():
            return self._send(404, {"ok": False, "error": "静态资源缺失"})
        data = asset.read_bytes()
        # sw.js 必须每次校验更新；图标/manifest 可安全缓存一天。
        cache = "no-cache" if filename == "sw.js" else "public, max-age=86400"
        if head_only:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return None
        return self._send(200, data, ctype, cache)

    def _access_payload(self) -> dict:
        """告诉使用者「别人该打开哪个地址」。地址来自真实网卡，不臆造。"""
        cfg = self.server.config
        host = str(cfg.get("host", "127.0.0.1"))
        port = int(self.server.server_address[1])
        on_all = host in ("0.0.0.0", "::", "")
        forwarded = self.headers.get("X-Forwarded-Proto", "").split(",")
        https = bool(forwarded) and forwarded[0].strip().lower() == "https"
        origins = [str(x) for x in (cfg.get("allowed_origins") or [])]
        public_mode = bool(cfg.get("secure_cookies")) or bool(https) \
            or any(x.startswith("https://") for x in origins)
        host_header = self.headers.get("Host", "")
        local_host = host_header.split(":")[0] in ("localhost", "127.0.0.1")
        # 公网模式下不回传内网拓扑，避免把内网地址泄露给外部访问者。
        lan = lan_addresses() if (on_all and not public_mode) else []
        notes: list[str] = []
        if not on_all:
            notes.append("服务只监听 127.0.0.1，他人设备现在打不开。" + lan_enable_hint())
        if on_all and not public_mode:
            notes.append("同一 WiFi 下，手机/他人电脑打开上面的局域网地址即可；"
                         "Windows 防火墙需放行 Python 的入站连接。")
            notes.append("局域网是 HTTP，浏览器不会启用 Service Worker；"
                         "手机可用「添加到主屏」存快捷方式，但不是可离线安装的 PWA。")
        if public_mode:
            notes.append("当前按公网模式运行：请确认已由反向代理终止 HTTPS，"
                         "并已设置邀请码限制注册。")
        notes.append("每人注册自己的账号；数据按账号隔离，换设备登录同一账号即可看到同一份进度。")
        return {
            "ok": True,
            "host": host, "port": port,
            "listening_on_all": on_all,
            "local_url": f"http://127.0.0.1:{port}/",
            "lan_urls": [f"http://{ip}:{port}/" for ip in lan],
            "lan_reachable_now": bool(on_all and lan and not public_mode),
            "public_mode": bool(public_mode),
            "invite_required": bool(cfg.get("invite_code")),
            "secure_cookies": bool(cfg.get("secure_cookies")),
            "pwa_installable": bool(public_mode or local_host),
            "enable_lan_hint": lan_enable_hint(),
            "notes": notes,
        }

    def _serve_ui(self) -> None:
        self._send(200, self.server.ui_html.encode("utf-8"),
                   "text/html; charset=utf-8")

    def do_HEAD(self) -> None:
        data = self.server.ui_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()

    def _auth(self, method: str, path: str) -> None:
        if path == "/api/auth/me" and method == "GET":
            user = self._current_user()
            if user is None:
                return self._send(401, {"ok": False, "error": "未登录"})
            return self._send(200, {"ok": True, "user": {
                "id": user["id"], "username": user["username"],
                "created_at": user["created_at"],
                "expires_at": user["expires_at"]}})
        if method != "POST":
            return self._send(405, {"ok": False, "error": "方法不允许"})
        if not self._origin_ok():
            return self._send(403, {"ok": False, "error": "跨站请求被拒绝"})
        try:
            body = self._read_json()
            ip = self.client_address[0] if self.client_address else ""
            if path == "/api/auth/register":
                user = self.server.auth.register(
                    body.get("username", ""), body.get("password", ""),
                    body.get("invite_code", ""), client_ip=ip)
                token = self.server.auth.login(
                    user["username"], body.get("password", ""), client_ip=ip)
                # 注册时把租户目录与初始存档建好，避免首次请求才发现缺文件。
                tenant = self.server.tenants.get(user["id"])
                self.server.tenants.release(tenant)
                self._cookie(token, self.server.auth.cookie_max_age)
                return self._send(201, {"ok": True, "user": user})
            if path == "/api/auth/login":
                token = self.server.auth.login(
                    body.get("username", ""), body.get("password", ""),
                    client_ip=ip)
                self._cookie(token, self.server.auth.cookie_max_age)
                return self._send(200, {"ok": True})
            if path == "/api/auth/logout":
                token = cookie_value(self.headers.get("Cookie"), "sid")
                self.server.auth.logout(token)
                self._cookie(None, 0)
                return self._send(200, {"ok": True})
            return self._send(404, {"ok": False, "error": "未知认证接口"})
        except AuthError as exc:
            return self._send(400, {"ok": False, "error": str(exc)})
        except (ValueError, TypeError, KeyError) as exc:
            return self._send(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"ok": False, "error": f"服务端异常: {exc}"})


class ProductServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, *, config: dict, auth: AuthStore,
                 tenants: TenantManager, ui_html: str) -> None:
        self.config = config
        self.auth = auth
        self.tenants = tenants
        self.ui_html = ui_html
        super().__init__(addr, handler)


def build_server(config: dict | None = None) -> ProductServer:
    cfg = config or load_config()
    auth = AuthStore(
        cfg["data_dir"], session_days=cfg["session_days"],
        max_users=cfg["max_users"], invite_code=cfg["invite_code"])
    tenants = TenantManager(
        cfg["data_dir"],
        tz_offset_seconds=cfg["default_tz_offset_seconds"],
        max_cached=cfg["max_cached_tenants"])
    ui_path = HERE / "ui" / "index.html"
    if not ui_path.exists():
        raise FileNotFoundError(f"缺少前端文件: {ui_path}")
    ui_html = ui_path.read_text(encoding="utf-8")
    return ProductServer((cfg["host"], cfg["port"]), ProductHandler,
                         config=cfg, auth=auth, tenants=tenants,
                         ui_html=ui_html)


def lan_addresses() -> list[str]:
    """枚举本机可用于局域网访问的 IPv4。全部来自系统查询，不做任何猜测。

    UDP connect 不发送数据包，只让内核选出通往该目标的出口网卡地址。
    """
    out: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_STREAM):
            out.add(info[4][0])
    except OSError:
        pass
    for probe in ("223.5.5.5", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 80))
            out.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return sorted(ip for ip in out
                  if ip and not ip.startswith("127.")
                  and not ip.startswith("169.254.") and ip != "0.0.0.0")


def main() -> None:
    cfg = load_config()
    server = build_server(cfg)
    port = int(server.server_address[1])
    on_all = str(cfg["host"]) in ("0.0.0.0", "::", "")
    public_mode = bool(cfg["secure_cookies"]) or any(
        str(x).startswith("https://") for x in (cfg.get("allowed_origins") or []))
    print(f"{PRODUCT_NAME} {PRODUCT_VERSION} 已启动")
    print(f"本机：http://127.0.0.1:{port}/")
    if on_all and not public_mode:
        ips = lan_addresses()
        if ips:
            print("他人电脑 / 手机（同一 WiFi）打开下面任一地址：")
            for ip in ips:
                print(f"    http://{ip}:{port}/")
        else:
            print("未查到局域网 IPv4，请检查网络连接；也可用 ipconfig / ifconfig 自查。")
    elif on_all and public_mode:
        print("公网模式：请通过 HTTPS 反向代理对外提供，不要直接暴露 8765 端口。")
    else:
        print("当前只监听本机，他人设备打不开。")
        print("要让同一 WiFi 的设备访问：" + lan_enable_hint())
    if on_all and not cfg.get("invite_code"):
        print("!! 警告：已对外监听但未设置邀请码，任何能连上的设备都能注册账号。")
        print("   设置 PROF_INVITE_CODE=你的邀请码 后重启。")
    if on_all and not cfg.get("secure_cookies") and not public_mode:
        print("提示：局域网为 HTTP，密码与会话不经加密；仅用于可信内网。")
    print(f"数据目录：{cfg['data_dir']}")
    print("按 Ctrl+C 停止；停止前会 flush 所有在线租户。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.tenants.flush_all()
        server.server_close()
        print("已停止，进度已写入各自存档。")


if __name__ == "__main__":
    main()
