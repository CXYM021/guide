# -*- coding: utf-8 -*-
"""单文件 exe 的启动入口。源码版也可以直接 `python frozen_entry.py` 运行。

职责（只做启动层，不碰任何熟练度判定）：
  1. 把可编辑配置、数据目录固定到 exe 同目录（临时解压目录会被删，不能放数据）。
  2. 首次运行生成 config.json；目录不可写时退回 %LOCALAPPDATA%。
  3. 端口被占用时自动顺延，并把实际端口告诉浏览器和控制台。
  4. 服务就绪后自动打开默认浏览器；PROF_NO_BROWSER=1 可关闭。
  5. 委托给 server.main()，保留其 Ctrl+C flush 行为。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import console  # noqa: E402,F401 - 导入即把控制台切成 UTF-8

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8765,
    "session_days": 30,
    "default_tz_offset_seconds": 28800,
    "max_users": 0,
    "invite_code": "",
    "secure_cookies": False,
    "max_cached_tenants": 64,
    "allowed_origins": [],
}


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def app_directory() -> Path:
    """exe 同目录；若不可写（例如放进 Program Files），退回用户数据目录。"""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    if _writable(base):
        return base
    fallback = Path(os.environ.get("LOCALAPPDATA")
                    or os.path.join(tempfile.gettempdir(), "proficiency-web"))
    target = fallback / "proficiency-web"
    target.mkdir(parents=True, exist_ok=True)
    print(f"[提示] 当前目录不可写，配置与数据改用：{target}")
    return target


def ensure_config(path: Path) -> dict:
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_CONFIG)
            merged.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
            return merged
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[警告] config.json 读取失败（{exc}），本次使用内置默认值")
            return dict(DEFAULT_CONFIG)
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"[首次运行] 已生成配置文件：{path}")
    return dict(DEFAULT_CONFIG)


def free_port(host: str, preferred: int) -> int:
    """从首选端口起找可绑定端口；返回 -1 表示连续 20 个都被占。"""
    bind_host = "0.0.0.0" if host in ("0.0.0.0", "::", "") else "127.0.0.1"
    for port in range(max(1, int(preferred)), max(1, int(preferred)) + 20):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((bind_host, port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    return -1


def open_when_ready(url: str) -> None:
    for _ in range(60):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=0.5) as resp:
                if resp.status == 200:
                    break
        except Exception:  # noqa: BLE001 - 服务还没起来是预期状态
            time.sleep(0.25)
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - 无桌面环境时不影响服务
        print(f"[提示] 未能自动打开浏览器：{exc}；请手动打开 {url}")


def main() -> None:
    # 让服务端的「怎样允许他人访问」提示说 exe 的做法（改 config.json），
    # 而不是源码版才有的 start_lan.bat。
    os.environ["PROF_EXE_MODE"] = "1"

    base = app_directory()
    cfg_path = Path(os.environ["PROF_CONFIG"]) if "PROF_CONFIG" in os.environ \
        else base / "config.json"
    cfg = ensure_config(cfg_path)
    os.environ["PROF_CONFIG"] = str(cfg_path)

    # 数据目录优先级：外部环境变量 > config.json 的 data_dir > exe 同目录 data/
    if "PROF_DATA_DIR" not in os.environ and not cfg.get("data_dir"):
        data_dir = base / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["PROF_DATA_DIR"] = str(data_dir)

    host = str(cfg.get("host", "127.0.0.1"))
    preferred = int(os.environ.get("PROF_PORT", cfg.get("port", 8765)))
    port = free_port(host, preferred)
    if port < 0:
        raise RuntimeError(f"端口 {preferred}–{preferred + 19} 都被占用，"
                           "请关闭占用程序或在 config.json 里换一个 port")
    if port != preferred:
        print(f"[提示] 端口 {preferred} 已被占用，本次改用 {port}")
    os.environ["PROF_PORT"] = str(port)

    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{browser_host}:{port}/"

    print("=" * 56)
    print(" 熟练度 · 修行录  —  下载即用版")
    print("=" * 56)
    print(f" 打开地址：{url}")
    print(f" 配置文件：{cfg_path}")
    print(f" 数据目录：{os.environ.get('PROF_DATA_DIR', '(见 config.json)')}")
    print(" 多人使用：把 config.json 的 host 改成 0.0.0.0 并设 invite_code")
    print(" 关闭这个窗口即停止服务；数据已落盘，下次打开还在。")
    print("=" * 56, flush=True)

    if os.environ.get("PROF_NO_BROWSER", "").lower() not in ("1", "true", "yes"):
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()

    import server  # noqa: E402 - 必须先设好环境变量再加载配置
    try:
        server.main()
    except Exception as exc:  # noqa: BLE001 - 双击运行时不能让窗口一闪而过
        print(f"\n[启动失败] {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        traceback.print_exc()
        if sys.stdin and sys.stdin.isatty():
            input("按回车键关闭…")
        sys.exit(1)
