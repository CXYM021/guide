# 打包成单文件 exe（Windows）

仓库里不放构建产物（`dist/` 已被 `.gitignore` 排除）。想给不会装 Python 的人用，
在 Windows 上跑一次下面这条命令，然后把 `dist/proficiency-web-win64.zip` 传到 Release。

```bat
python build_exe.py
```

`build_exe.py` 做四件事：

1. 如果系统里没有 PyInstaller，把固定版本 `6.22.3` 用 `pip install --target` 装进临时目录，
   构建完删掉，不写系统 `site-packages`。
2. 用标准库把 `ui/icon-192.png` 打成 `ui/app.ico`（ICO 内嵌 PNG，不需要 Pillow）。
3. 调 PyInstaller onefile，把入口 `frozen_entry.py`、`server.py` / `auth.py` / `tenant.py`、
   `engine/` 全部模块、`ui/`（index.html、manifest、图标、sw.js）、`config.example.json` 打进一个 exe。
4. 生成 `README.txt`、附上 CPython 许可证、算 SHA-256、打成 zip。

构建期需要联网（只为了取 PyInstaller）；产物运行时零第三方依赖。

## 验收

```bat
python smoke_exe.py
```

`smoke_exe.py` 会起一个隔离的数据目录，跑注册、登录、练习、导出、备份、退出等 20 多项，
不会碰你正在用的 `data/`。全部 PASS 才算能发。

## 版本与校验

- 版本号写在 `server.py` 的 `VERSION`，发布前同时更新 `CHANGELOG.md`。
- `SHA256.txt` 是 zip 里 exe 的校验值，Release 说明里应贴同一串。
- 改了 `engine/` 或 `ui/` 之后：先 `python build_ui.py`（改过前端模板时），
  再 `python build_exe.py`，最后 `python smoke_exe.py`。

## 其他平台

Linux / macOS / 服务器用源码跑：`python server.py`、`./start.sh`、
`docker compose up -d`，见 [deploy.md](deploy.md)。PyInstaller 也能交叉出对应平台的包，
但本仓库的脚本只针对 Windows 调过。
