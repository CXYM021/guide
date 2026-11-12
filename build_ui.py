# -*- coding: utf-8 -*-
"""从 engine/web_ui.py 的 HTML_PAGE + ui/auth_shell.html 重建 ui/index.html。

改了前端或认证壳之后运行：python build_ui.py
这样产品目录完全自包含，不依赖开发版工作区。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_DIR = HERE / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import console  # noqa: E402,F401 - 导入即把控制台切成 UTF-8

from web_ui import HTML_PAGE  # noqa: E402


# 手机「添加到主屏」所需的 head 标签。引擎模板是开发版共用的，
# 产品专属标签在构建期注入，不污染开发版、不破坏逐字节同源校验。
PWA_HEAD = (
    '<link rel="manifest" href="/manifest.webmanifest">\n'
    '<link rel="apple-touch-icon" href="/icon-192.png">\n'
    '<link rel="icon" type="image/png" href="/icon-192.png">\n'
    '<meta name="theme-color" content="#3f3a8f">\n'
    '<meta name="mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="default">\n'
)


def product_version() -> str:
    text = (HERE / "server.py").read_text(encoding="utf-8")
    hit = re.search(r'PRODUCT_VERSION\s*=\s*"([^"]+)"', text)
    if not hit:
        raise ValueError("server.py 里找不到 PRODUCT_VERSION")
    return hit.group(1)


def sync_service_worker(version: str) -> bool:
    """把 sw.js 的缓存名对齐到版本号：每次发版都换一个壳缓存，免得旧页面赖着不走。"""
    path = HERE / "ui" / "sw.js"
    text = path.read_text(encoding="utf-8")
    new = re.sub(r"guide-shell-[0-9A-Za-z.\-]+",
                 "guide-shell-" + version, text, count=1)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8", newline="\n")
    return True


def build() -> str:
    for name in ("auth_shell.html", "manifest.webmanifest", "sw.js",
                 "icon-192.png", "icon-512.png"):
        if not (HERE / "ui" / name).is_file():
            raise FileNotFoundError(
                f"缺少产品资源 ui/{name}；静态资源请先运行 python build_assets.py")
    shell = (HERE / "ui" / "auth_shell.html").read_text(encoding="utf-8")
    if "<div id=\"auth-mask\"" not in shell:
        raise ValueError("认证壳模板缺少 auth-mask 节点")
    html = HTML_PAGE.replace(
        "\nrefresh();\n</script>",
        "\nif (!window.__PRODUCT_AUTH__) { refresh(); }\n</script>", 1)
    if "window.__PRODUCT_AUTH__" not in html:
        raise ValueError("HTML_PAGE 结构变化，无法注入启动保护")
    version = product_version()
    html = html.replace(
        "</head>",
        '<script>window.__PRODUCT_AUTH__ = true; window.__APP_VERSION__ = "'
        + version + '";</script>\n' + PWA_HEAD + "</head>", 1)
    html = html.replace("</body>", shell.rstrip() + "\n</body>", 1)
    return html


def main() -> None:
    out = HERE / "ui" / "index.html"
    old = out.read_text(encoding="utf-8") if out.exists() else None
    html = build()
    out.write_text(html, encoding="utf-8")
    sw_changed = sync_service_worker(product_version())
    status = "无变化" if old == html else "已更新"
    if sw_changed:
        status += "；sw.js 缓存名已对齐版本"
    print(f"{out} {status}（{len(html.encode('utf-8'))} bytes）")


if __name__ == "__main__":
    main()
