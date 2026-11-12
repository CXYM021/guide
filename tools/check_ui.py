# -*- coding: utf-8 -*-
"""检查 ui/index.html 是否与 engine/web_ui.py 的模板同源（CI 与本地都能跑）。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


import console  # noqa: E402,F401 - 导入即把控制台切成 UTF-8


def main() -> int:
    import build_ui

    current = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    built = build_ui.build()
    if built != current:
        print("ui/index.html 与 engine/web_ui.py 不同源，请运行 python build_ui.py",
              file=sys.stderr)
        return 1
    print("ui/index.html 与模板同源")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
