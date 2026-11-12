# -*- coding: utf-8 -*-
"""把控制台输出统一成 UTF-8，避免中文提示在非 UTF-8 代码页下抛 UnicodeEncodeError。

背景：Windows 的 CI 和「输出被重定向到管道/文件」的场景，Python 会按系统代码页
（英文系统是 cp1252）编码 stdout，本项目的启动提示是中文，`print` 会直接抛
`UnicodeEncodeError: 'charmap' codec can't encode characters`，把整条命令带崩。
真实用户从计划任务、服务包装器或日志重定向启动时也会遇到同样的问题。

做法：导入本模块时，把 stdout/stderr 的编码改成 UTF-8（编码失败的字符用 `?`
代替，绝不抛异常）。已经是 UTF-8 的流保持原样，控制台里仍是正常中文。
"""

from __future__ import annotations

import sys


def setup() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # pythonw / 无控制台的打包形态
            continue
        try:
            encoding = (stream.encoding or "").lower().replace("-", "")
        except AttributeError:  # 被测试框架替换成 StringIO 之类
            continue
        if encoding == "utf8":
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            continue


setup()
