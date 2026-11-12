# -*- coding: utf-8 -*-
"""下载即用版的可重复验收：把 zip 当刚下载的文件解压、实跑、重启、清理。

运行：python smoke_exe.py
      python smoke_exe.py --zip dist/guide-web-win64.zip
退出码 0 = 全部通过，非 0 = 失败（打印失败原因）。

刻意不用 unittest：这一步要求真的起 Windows 进程、真的占用端口、真的
写磁盘，属于端到端冒烟，不能和纯函数单测混在一起。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import console  # noqa: E402,F401 - 导入即把控制台切成 UTF-8

HERE = Path(__file__).resolve().parent
EXPECTED_ZIP_FILES = {"guide-web.exe", "README.txt",
                      "PYTHON-LICENSE.txt", "SHA256.txt"}


def call(port: int, path: str, body=None, cookie: str | None = None):
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers,
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode()), resp.headers
            except (UnicodeDecodeError, json.JSONDecodeError):
                return resp.status, raw, resp.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode()), exc.headers
        except (UnicodeDecodeError, json.JSONDecodeError):
            return exc.code, raw, exc.headers


def kill_tree(proc: subprocess.Popen) -> None:
    """onefile exe 有 bootloader 父进程 + 子进程，必须整棵树一起结束。"""
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait(timeout=10)


def port_open(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.3)
    result = sock.connect_ex(("127.0.0.1", port))
    sock.close()
    return result == 0


def no_process() -> bool:
    raw = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq guide-web.exe"],
        capture_output=True).stdout
    return b"guide-web.exe" not in raw


def launch(root: Path, exe: Path, name: str):
    log = root / name
    handle = log.open("a", encoding="utf-8", errors="replace")
    env = dict(os.environ, PROF_NO_BROWSER="1", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([str(exe)], cwd=str(root), env=env,
                            stdout=handle, stderr=subprocess.STDOUT)
    for _ in range(120):
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        match = re.search(r"打开地址：http://127\.0\.0\.1:(\d+)/", text)
        if match:
            port = int(match.group(1))
            try:
                if call(port, "/healthz")[0] == 200:
                    return proc, handle, port, log
            except Exception:  # noqa: BLE001 - 还没监听是预期状态
                pass
        time.sleep(0.2)
    kill_tree(proc)
    handle.close()
    raise RuntimeError(f"exe 未就绪，日志：\n{text[:1500]}")


def wait_port_closed(port: int) -> bool:
    for _ in range(50):
        if not port_open(port):
            return True
        time.sleep(0.2)
    return False


def check(condition: bool, label: str) -> None:
    print(("  OK   " if condition else "  FAIL ") + label)
    if not condition:
        raise AssertionError(label)


def run(zip_path: Path) -> None:
    check(zip_path.is_file(), f"分发包存在：{zip_path}")
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        check(names == EXPECTED_ZIP_FILES, f"包内文件齐全且无多余：{sorted(names)}")

    td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    root = Path(td.name) / "下载测试 中文目录"   # 顺带验证中文路径
    root.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(root)
    exe = root / "guide-web.exe"
    recorded = (root / "SHA256.txt").read_text(encoding="utf-8").split()[0]
    actual = hashlib.sha256(exe.read_bytes()).hexdigest()
    check(actual == recorded, "SHA256 与包内记录一致")

    cookie = ""
    p1 = f1 = p2 = f2 = None
    try:
        p1, f1, port1, log1 = launch(root, exe, "run1.log")
        check((root / "config.json").is_file(), "首次运行生成 config.json 到 exe 同目录")
        check((root / "data").is_dir(), "数据目录生成到 exe 同目录")

        code, ui, _ = call(port1, "/")
        check(code == 200 and b"auth-mask" in ui and b'rel="manifest"' in ui,
              "登录页含认证壳与 PWA 标签")
        check(call(port1, "/manifest.webmanifest")[0] == 200, "manifest 可访问")
        check(call(port1, "/icon-192.png")[0] == 200, "图标可访问")

        code, reg, headers = call(
            port1, "/api/auth/register",
            {"username": "下载验收", "password": "password123"})
        check(code == 201 and reg.get("ok") is True, "注册账号成功")
        cookie = str(headers.get("Set-Cookie", "")).split(";")[0]
        code, custom, _ = call(port1, "/api/skills/custom", cookie=cookie)
        check(code == 200 and custom.get("ok") and custom.get("skills") == [],
              "自定义技能模块已打进 exe 且按账号隔离")

        code, res, _ = call(port1, "/api/practice", {
            "skill_id": "英语口语", "difficulty": 3, "quality": 80,
            "feedback": "external", "task_key": "T-smoke",
            "metrics": {"continuous_minutes": 12, "active_phrases": 8,
                        "listener_score": 85}}, cookie=cookie)
        check(code == 200 and res["verdict"]["valid"] and res["verdict"]["gain"] == 25,
              "练习有效且增益与源码版一致（+25）")
        first = call(port1, "/api/skills", cookie=cookie)[1]
        check(first["verify"] == [] and len(first["events"]) == 1,
              "一致性自检为空、事件序号连续")

        custom_name = "示例自定义技能"
        code, added, _ = call(port1, "/api/skills/custom/add", {
            "name": custom_name, "source_urls": [],
            "metrics": [{"key": "practice_minutes", "label": "练习时长",
                         "unit": "分钟", "range_low": 0, "range_high": 120}],
        }, cookie=cookie)
        check(code == 200 and added.get("ok")
              and added["provenance"]["ai_used"] is False,
              "exe 内能新增自定义技能且明示为本地模板")
        code, custom_practice, _ = call(port1, "/api/practice", {
            "skill_id": custom_name, "difficulty": 3, "quality": 80,
            "feedback": "external", "task_key": "自定义练习",
            "metrics": {"practice_minutes": 90},
        }, cookie=cookie)
        check(code == 200 and custom_practice["verdict"]["valid"],
              "自定义技能在 exe 里能按指标门判定并加分")
        first = call(port1, "/api/skills", cookie=cookie)[1]
        check(first["verify"] == [], "加入自定义技能后一致性自检仍为空")

        acc = call(port1, "/api/access")[1]
        check("config.json" in acc.get("enable_lan_hint", ""),
              "exe 模式的他人访问提示指向 config.json")
    finally:
        if p1 is not None:
            kill_tree(p1)
            f1.close()

    try:
        port1 = int(re.search(
            r"打开地址：http://127\.0\.0\.1:(\d+)/",
            (root / "run1.log").read_text(encoding="utf-8", errors="replace")).group(1))
        check(wait_port_closed(port1) and no_process(), "旧进程整棵树退出、端口释放")

        p2, f2, port2, log2 = launch(root, exe, "run2.log")
        banner = log2.read_text(encoding="utf-8", errors="replace")
        check("下载即用版" in banner and "[首次运行]" not in banner,
              "第二次启动是全新进程且没有覆盖已有配置")
        check(call(port2, "/api/auth/me", cookie=cookie)[0] == 200,
              "会话跨重启有效")
        second = call(port2, "/api/skills", cookie=cookie)[1]
        check(second["verify"] == []
              and second["achievement_context"]["total_xp"] == first["achievement_context"]["total_xp"]
              and len(second["events"]) == len(first["events"])
              and second["events"][0]["task_key"] == first["events"][0]["task_key"],
              "重启后同一账号的进度完整保留")
        code, custom2, _ = call(port2, "/api/skills/custom", cookie=cookie)
        check(code == 200 and any(x["skill_id"] == custom_name
                                  for x in custom2["skills"]),
              "重启后自定义技能定义与来源记录仍在")
    finally:
        if p2 is not None:
            kill_tree(p2)
            f2.close()
            check(no_process(), "第二次启动的进程整棵树已退出")

    for _ in range(5):
        try:
            shutil.rmtree(root)
            break
        except OSError:
            time.sleep(0.6)
    check(not root.exists(), "临时目录已清理、无残留")
    td.cleanup()
    print("\n全部通过：这个 zip 可以发给别人，解压双击即可用。")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", default=str(HERE / "dist" / "guide-web-win64.zip"))
    args = parser.parse_args()
    try:
        run(Path(args.zip).resolve())
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n验收失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
