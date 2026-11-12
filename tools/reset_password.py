# -*- coding: utf-8 -*-
"""在本机重置某个账号的密码。

没有邮箱验证，所以「忘记密码」只能由能访问 data/ 目录的人在服务器上执行。
重置后该账号的所有登录会话会被清掉（旧会话可能已经在别人手里）。

用法::

    python tools/reset_password.py 用户名
    python tools/reset_password.py 用户名 --password "新的密码"
    python tools/reset_password.py 用户名 --data-dir "D:\\proficiency\\data"

不带 --password 时会交互输入两次，不会留在命令历史里。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import console  # noqa: E402,F401 - 导入即把控制台切成 UTF-8

from auth import AuthError, AuthStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重置本机账号密码")
    parser.add_argument("username", help="账号名（大小写/全角/零宽字符都会自动归一）")
    parser.add_argument("--password", help="新密码；不填则交互输入")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("PROF_DATA_DIR") or str(ROOT / "data"),
        help="数据目录（默认 ./data，也可用环境变量 PROF_DATA_DIR）",
    )
    args = parser.parse_args(argv)

    password = args.password
    if password is None:
        password = getpass.getpass("新密码：")
        again = getpass.getpass("再输一次：")
        if password != again:
            print("两次输入不一致，没有改动。", file=sys.stderr)
            return 2

    store = AuthStore(args.data_dir)
    try:
        user = store.set_password(args.username, password)
    except AuthError as exc:
        print(f"没有改动：{exc}", file=sys.stderr)
        return 1
    print(f"已重置 {user['username']} 的密码；该账号的旧会话已失效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
