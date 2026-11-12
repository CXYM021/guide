# -*- coding: utf-8 -*-
"""构建「下载即用」的 Windows 单文件 exe 与分发 zip。

运行：python build_exe.py
产物：dist/proficiency-web.exe
      dist/proficiency-web-win64.zip（exe + 说明 + Python 许可证 + SHA256）

构建期需要联网把 PyInstaller 装进临时目录（不污染系统 Python，不写全局
site-packages）；构建完临时目录自动删除。产物运行时零第三方依赖、不需要
安装 Python。源码版、Docker 部署方式完全不受影响。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
WORK = HERE / ".build"
PYINSTALLER_VERSION = "6.22.3"  # 固定版本，保证重建结果可复现

HIDDEN_IMPORTS = [
    "server", "web_app", "web_ui", "auth", "tenant",
    "proficiency", "realistic_rules", "capture_ledger",
    "benchmark", "data_vault", "world_state", "sqlite3",
]

README = """熟练度 · 修行录 —— 下载即用版（Windows 64 位）
================================================

一、怎么用
----------
1. 把 proficiency-web.exe 放到一个你找得到的文件夹（桌面或新建文件夹都行）。
2. 双击它。会弹出一个黑色窗口，稍等 2–5 秒，浏览器自动打开系统页面。
3. 注册一个账号就能用。每人一个账号，数据互相看不见。
4. 用完直接关掉黑窗口即停止服务。数据已存盘，下次双击还在。

不需要安装 Python，不需要装数据库，不需要联网，不需要命令行。
第一次启动会解压内置运行时到临时目录，所以比普通小程序慢几秒，是正常的。

二、我的数据在哪
----------------
exe 同目录下会自动生成：
  config.json   可改的配置文件（用记事本打开）
  data\\          全部账号与进度，app.db 是账号密码库，users\\ 下是每人存档
  data\\users\\<ID>\\custom_skills.json  该用户自定义技能的规则与来源记录
  data\\users\\<ID>\\backups\\  每日滚动备份，保留 14 天
备份 = 复制整个 data 文件夹。换电脑 = 把 exe、config.json、data 一起拷走。
设置页「导出备份」只导出当前账号的进度与自定义技能，不含账号密码库 app.db；
换电脑后要在新机器上重新注册再导入，或者直接把整个 data 拷过去。
升级 = 只替换 exe，保留 config.json 和 data。

如果 exe 放在不可写的目录（例如 C:\\Program Files），程序会改用
%LOCALAPPDATA%\\proficiency-web\\ 存配置和数据，启动窗口会打印实际路径。

三、让别人的电脑 / 手机也能用
------------------------------
1. 用记事本打开 config.json，把 "host": "127.0.0.1" 改成 "host": "0.0.0.0"，
   再给 "invite_code" 填一个邀请码（不让陌生人随便注册）。
2. 重新双击 exe。窗口会打印真实的局域网地址，例如 http://192.168.1.7:8765/ 。
3. 同一 WiFi 下的手机 / 电脑打开这个地址，各自注册账号。
   Windows 首次会弹防火墙，勾「专用网络」并允许。
4. 这台电脑必须开着、黑窗口不能关。关掉别人就连不上，但数据不会丢。
5. 局域网是 HTTP 明文，只在家用/可信内网用；不要把这个端口映射到公网。
   异地使用请用源码包里的 deploy/ 方案（域名 + HTTPS 反向代理）。

登录后页面右上角「他人访问」按钮可以直接复制这个地址。

四、常见情况
------------
* 端口 8765 被占用：程序自动顺延到 8766 等，窗口会打印实际端口。
* 不想自动开浏览器：设环境变量 PROF_NO_BROWSER=1 后启动。
* Windows SmartScreen 或杀毒软件提示「未知发布者」：这个 exe 没有代码签名
  证书（签名要付费证书），不是病毒。是否放行由你自己判断；不放心就用
  源码包自己 python server.py 跑，结果完全一样。
* 手机「添加到主屏」：局域网 HTTP 下只能是快捷方式，主机必须在线；
  真正可离线安装的 PWA 需要 HTTPS，见源码包 deploy/。
* 关卡、数值、问卷与源码版完全一致：引擎文件逐字节同源，exe 只是套了
  一层启动器和自带 Python 运行时。

五、自己加技能 / 可选的 AI 联网生成
------------------------------------
设置页「我的技能」可以增删自定义技能：填技能名与 1–5 个可观测指标
（英文标识、名称、单位、合法下界与上界），系统生成指标门与 LV1–LV8
达标要求，之后按原有全部判定门练习、挂机结算、导出备份。

默认不联网、不内置大模型：新增技能用本地保守模板生成，界面会明确写
「本地模板（未联网、不冒充 AI 判定）」，来源记录里也写明原因。

要让 AI 依据网络资料生成升级机制，需自己准备一个 OpenAI 兼容端点，
在启动 exe 的那个命令行窗口先执行（密钥不要写进 config.json）：

  set PROF_AI_ENDPOINT=https://你的端点/v1
  set PROF_AI_KEY=你的密钥
  set PROF_AI_MODEL=你的模型名
  proficiency-web.exe

然后在添加技能时填 1–3 个公开资料网址。服务端会抓取正文（限 256KB、
禁重定向、默认拒绝内网地址防 SSRF），连同严格 JSON 结构要求交给模型；
模型只能在用户给定的合法范围内决定最低线、优秀线、各级要求与描述，
返回值逐项校验，越界或字段不全一律回退本地模板并写明原因。
生成的是静态规则，落盘后每次练习不再联网、不再调用模型，结果可复现。
界面会标出「AI 联网生成」还是「本地模板」，来源记录含网址、正文哈希、
模型名与时间。自报数值仍然不验真。

六、边界
--------
仅在本机 Windows x64（Python 3.13.14 / CPython 64 位）实跑通过。
Linux、macOS、其他架构请用源码包 product/（python server.py 或 Docker）。
单进程多线程：不要同时开两个 exe 指向同一个 data 目录。
"""

LICENSE_NOTE = """本分发包内含 CPython 运行时（Python 软件基金会许可）。
完整许可证见同目录 PYTHON-LICENSE.txt。
本程序自身的熟练度规则与代码为原项目内容，未引入第三方运行时依赖。
"""


def make_ico(png_path: Path, ico_path: Path) -> None:
    """ICO 目录 + 内嵌 PNG（Vista 及以上支持），纯标准库，无需 Pillow。"""
    png = png_path.read_bytes()
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 192, 192, 0, 0, 1, 32, len(png), 22)
    ico_path.write_bytes(header + entry + png)


def ensure_pyinstaller() -> tuple[str, tempfile.TemporaryDirectory | None]:
    try:
        import PyInstaller  # noqa: F401
        print("使用已安装的 PyInstaller")
        return "", None
    except ImportError:
        pass
    tmp = tempfile.TemporaryDirectory(prefix="proficiency-pyi-")
    print(f"构建期临时安装 PyInstaller {PYINSTALLER_VERSION} 到 {tmp.name}")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--no-warn-script-location", "-q", "--target", tmp.name,
         f"pyinstaller=={PYINSTALLER_VERSION}"],
        check=True, timeout=600)
    return tmp.name, tmp


def build_exe(tools_dir: str) -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    ico = HERE / "ui" / "app.ico"
    make_ico(HERE / "ui" / "icon-192.png", ico)

    hidden = [arg for mod in HIDDEN_IMPORTS
              for arg in ("--hidden-import", mod)]
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "proficiency-web",
        "--distpath", str(DIST), "--workpath", str(WORK / "work"),
        "--specpath", str(WORK),
        "--paths", str(HERE), "--paths", str(HERE / "engine"),
        "--add-data", f"{HERE / 'ui'};ui",
        "--add-data", f"{HERE / 'engine'};engine",
        "--add-data", f"{HERE / 'config.example.json'};.",
        "--icon", str(ico),
        "--exclude-module", "tkinter",
        *hidden,
        str(HERE / "frozen_entry.py"),
    ]
    env = dict(os.environ)
    if tools_dir:
        env["PYTHONPATH"] = tools_dir + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run(cmd, cwd=str(HERE), env=env, check=True, timeout=900)
    exe = DIST / "proficiency-web.exe"
    if not exe.is_file():
        raise FileNotFoundError(f"构建结束但没找到 {exe}")
    return exe


def package(exe: Path) -> Path:
    readme = DIST / "README.txt"
    readme.write_text(README, encoding="utf-8")
    lic = DIST / "PYTHON-LICENSE.txt"
    py_license = Path(sys.executable).parent / "LICENSE.txt"
    if py_license.is_file():
        lic.write_bytes(py_license.read_bytes())
    else:
        lic.write_text(LICENSE_NOTE, encoding="utf-8")
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (DIST / "SHA256.txt").write_text(
        f"{digest}  proficiency-web.exe\n", encoding="utf-8")

    zip_path = DIST / "proficiency-web-win64.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in ("proficiency-web.exe", "README.txt",
                     "PYTHON-LICENSE.txt", "SHA256.txt"):
            z.write(DIST / name, name)
    print(f"exe   : {exe}  {exe.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"zip   : {zip_path}  {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"sha256: {digest}")
    return zip_path


def main() -> None:
    tools_dir, tmp = ensure_pyinstaller()
    try:
        exe = build_exe(tools_dir)
        package(exe)
    finally:
        if tmp is not None:
            tmp.cleanup()
        if WORK.exists():
            shutil.rmtree(WORK, ignore_errors=True)
    print("完成。把 zip 发出去，对方解压双击 exe 即可。")


if __name__ == "__main__":
    main()
