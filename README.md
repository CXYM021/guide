# 向导（熟练度系统） · Guide

[![tests](https://github.com/CXYM021/guide/actions/workflows/tests.yml/badge.svg)](https://github.com/CXYM021/guide/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**[⬇ 下载 Windows 版（免安装 exe）](https://github.com/CXYM021/guide/releases/latest/download/guide-web-win64.zip)**
· 解压双击 `guide-web.exe` 就能用，不需要 Python。

> 别点右上角绿色的 `Code → Download ZIP`：那是**源码**，里面没有 exe。
> 可执行文件只放在 [Releases](https://github.com/CXYM021/guide/releases) 里。

把「我在变强」变成可以核对的数据：七项技能、LV1–LV8、只记有效练习、停练会掉、能复测。

一个可以完全跑在自己电脑上的技能熟练度账本。没有排行榜、没有金币、没有「打卡即加分」——
每一次记录都要过判定门，等级不是签到天数，是「最近这段时间你确实做到了什么」。

- **只认有效练习**：质量太低、连续挑简单任务、同一个任务机械重复，都不计分
- **等级有门槛**：经验够了还要过「最近窗口质量 + 客观度量 + 技能指标」三关
- **会掉**：停练按天衰减保持度，重新开始有恢复机制，不是一次练完永久有效
- **数据是你的**：单文件 SQLite + 每用户 JSON，一键导出，零遥测、零联网（AI 生成技能是可选开关）
- **纯标准库**：Python ≥ 3.10，不需要 pip install 任何东西

> 它衡量的是「可复核的进步」，不是「认证」。所有练习数据都是自报，系统不验真
> （模型里 `trusted=False` 是明确写死的）。别把它当简历，把它当仪表盘。

---

## 快速开始

### Windows：下载即用

从 [Releases](../../releases) 下载 `guide-web-win64.zip`，解压到任意文件夹，双击
`guide-web.exe`。浏览器会自动打开 `http://127.0.0.1:8765/`，注册一个账号就能用。
不需要装 Python、不需要联网。数据和配置落在 exe 同目录的 `data/`、`config.json`。

> 这个 exe 没有代码签名，Windows 第一次运行可能弹「已保护你的电脑」。点「更多信息 →
> 仍要运行」即可；介意的话用下面的源码方式运行，行为完全一致。校验和见 Release 说明。

### 源码运行（Windows / macOS / Linux）

```bash
python server.py          # 默认 http://127.0.0.1:8765/
# 或
./start.sh                # Linux/macOS
start.bat                 # Windows
```

要求 Python 3.10+，无第三方依赖。

### Docker

```bash
docker compose up -d      # 打开 http://127.0.0.1:8765/
```

### 给同一 WiFi 下的手机 / 别人的电脑用

```bash
PROF_INVITE_CODE=你的邀请码 ./start_lan.sh     # Windows: start_lan.bat
```

启动后会打印真实网卡地址（例如 `http://192.168.1.23:8765/`），手机连同一个 WiFi 打开即可，
各自注册账号，数据互相隔离。**局域网是 HTTP，只建议在可信网络里用**；公网部署见
[docs/deploy.md](docs/deploy.md)。

### 在手机上长期用（不必每次开电脑）

网页版本身就是移动端优先的，手机上完全能用。区别只在于「谁在跑服务」：

| 做法 | 手机怎么打开 | 代价 |
| --- | --- | --- |
| 同一 WiFi（现在就可用） | `start_lan.bat` 起的地址，手机浏览器存书签 | 电脑要开着 |
| 公网 + 域名 | 域名即可，随时可用 | 一台常开的机器 + 域名 + HTTPS（`deploy/` + [docs/deploy.md](docs/deploy.md)） |
| 内网穿透（不想买服务器） | Cloudflare Tunnel 之类的免费隧道，给你一个 https 地址 | 电脑仍需开着，但不用公网 IP、不用备案 |

手机上打开后可以「添加到主屏」，点开就像个 App。**HTTPS 下才是真正可离线安装的 PWA**；
局域网 HTTP 只能是快捷方式。手机端没有键盘也能用「补记一次」记线下练习，不必挂机凑时长。

> 小程序（微信）没有做：它要求企业主体、HTTPS 域名、审核，而且小程序里跑不了本地 Python，
> 后端必须搬到云上——那等于换一个产品。上面「公网 + 域名」已经能得到同样的手机体验，
> 成本低得多。要不要做小程序，等你确认有真实用户再说。

---

## 第一次使用（大约 5 分钟）

1. 注册后先答一份 **分技能自评**：英语按 CEFR/IELTS、西语按 DELE、跑步按 VDOT 成绩表、健身按相对力量、办公/剪辑按交付物——每项都问「做得到的事实」，不问「你觉得自己怎么样」。自评最高给到 LV6，只抬高等级下限，不送经验；LV7 起仍要复测与客观成绩。
2. 今日页会提示「三步开始」：选一个技能 → 记一次修行 → 看增益拆解。
3. 记录时照实填：**难度**（1 很轻松 … 5 拼尽全力）、**质量**（0–100）、**反馈来源**。
4. 提交后会看到「本次增益拆解」：难度匹配、完成质量、反馈强度、重复折扣、同日递减、间隔加成。
   哪一项系数最低，下一次就补哪一项——这是系统唯一想教你的东西。
5. 每次记录还有一份 **4 题当次问卷**（专注/疲劳/新鲜度/完成度），在 0.80–1.12 之间微调这次收益；
   不填就是 ×1.00，不惩罚。

---

## 它到底怎么算分（诚实版）

一次练习要同时满足这些才算「有效」：

| 条件 | 规则 |
| --- | --- |
| 质量下限 | 质量 < 40 直接拒绝 |
| 无反馈上限 | 反馈为「无」时，质量必须 ≥ 70 |
| 难度匹配 | 连续 3 次明显低于当前等级带，拒绝 |
| 机械重复 | 同名任务在窗口内重复，第二次起收益 ×0.2（占位名不参与判定） |
| 技能指标 | 有专属指标的技能（如英语口语）必须填齐且不低于下限 |

增益的骨架是「基础值 × 难度 × 质量 × 反馈 × 重复 ≈ 20 点上下」，等级成本按幂律增长
（LV1→2 约 100 点，LV7→8 约 1800 点）。等级门槛还要求最近窗口的平均质量、至少一次客观度量、
以及技能指标达标——这就是为什么会出现「经验够了但卡在 LV4」：它在逼你换一种练法，不是坏了。

停练后保持度按天衰减（下限 35%），所以「等级掉了」通常意味着「手生了」，而不是系统扣你分。

规则全部在 `engine/realistic_rules.py` 里，是纯函数，不读网络、不读时钟以外的状态。

---

## 功能一览

- **七项内置技能**：英语口语、英语读写、跑步技术、健身技术、西班牙语、电脑办公软件技术、剪辑技术
- **自定义技能**：自己加/删（上限 20 个），定义 1–5 个可量化指标；升级要求默认用
  本地「通用起步标准」，也可以在设置页配一个 AI 服务，先查公开资料再折算门槛，
  来源与是否用过 AI 都写在记录里
- **分技能自评 + 每次当次问卷**：题库按技能换，问的是考试分数、比赛成绩、作品与交付，不是性格测试；只用来定起点和微调单次收益
- **今日最优一手**：按「离升级最近 / 生疏 / 最久没练 / 最大短板」推荐下一次练什么
- **基准复测**：同一任务隔 ≥28 天重测，只认客观度量，不写经验、不改保持度
- **江湖（利他层）**：护道对象是**这台服务器上的真实账号**（互加好友后可见「今天练没练」）；
  本机预置的三份演示档案收进「练手样本」，明确标注不是真人、不会回复
- **数据主权**：导出/导入、每日滚动备份（保留 14 天，长驻进程跨天也会补）、时区可切
- **多用户**：账号 + 会话 + 每用户独立目录与锁；同一台机器上互不可见
- **PWA**：HTTPS 下可安装到主屏（Service Worker 只缓存外壳，绝不缓存 API）
- **开发者工具默认关闭**：推进时间、改曲线、直接加经验都要先在设置里显式打开

---

## 配置

复制 `config.example.json` 为 `config.json`，或用环境变量（优先级更高）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PROF_HOST` | `127.0.0.1` | 监听地址；改 `0.0.0.0` 前请先设邀请码 |
| `PROF_PORT` | `8765` | 端口 |
| `PROF_DATA_DIR` | `./data` | 数据目录 |
| `PROF_INVITE_CODE` | 空 | 注册邀请码；对外开放时必填 |
| `PROF_MAX_USERS` | `0` | 0 = 不限制 |
| `PROF_SECURE_COOKIES` | `0` | HTTPS 下设为 `1` |
| `PROF_ALLOWED_ORIGINS` | 空 | 逗号分隔，如 `https://skill.example.com` |
| `PROF_SESSION_DAYS` | `30` | 会话有效期 |
| `PROF_TZ_OFFSET` | `28800` | 新账号默认时区（秒） |
| `PROF_CONFIG` | – | 指定另一个配置文件路径 |
| `PROF_AI_ENDPOINT` / `PROF_AI_KEY` / `PROF_AI_MODEL` | 空 | 可选：OpenAI 兼容端点（优先级高于设置页里填的） |

### 配 AI（可选，不配也能用）

设置页「我的技能 → 标准从哪来」里直接填：选一个预设（DeepSeek / 通义千问 / Kimi /
OpenAI / 自定义），粘 API Key，点「测试一下」，通过就保存。不用命令行，也不用重启。

- 钥匙存 `data/users/<ID>/ai_secret.json`，界面只回掩码（`sk-…` 加后 4 位），
  不写日志、不进导出包、不回传给前端。
- 不想让钥匙落盘：改用上面的三个环境变量，环境变量优先级最高。
- 不配 AI 时，新增技能走本地「通用起步标准」，界面明写「本地模板·未用过 AI」。
- AI 只在你添加技能的那一刻用一次（把公开资料折算成 LV1–LV8 门槛），之后每次练习
  都按已落盘的静态规则在本地判定，不联网、可复现。

### 忘记密码

没有邮箱验证，找回只能在服务器上做（需要能访问 `data/`）：

```bash
python tools/reset_password.py 用户名
```

会交互输入新密码，并清掉该账号的所有旧会话。

### 升级后页面还是旧的？

浏览器把网页外壳交给 Service Worker 缓存，升级那一次可能还拿着旧页面。
从 1.0.4 起外壳改为网络优先，并且页面会自己比对服务端版本：
**升级后再刷新一次**就会自动换新。如果仍然报错，按 F12 →
Application → Service Workers → Unregister，或清除该站点数据后重开。
存档在 `data/`，清浏览器缓存不会动它。

---

## 开发

```bash
python -m unittest discover -s tests -p "test_*.py" -v   # 88 条测试
python build_ui.py        # 改了 engine/web_ui.py 之后重建 ui/index.html
python build_exe.py       # Windows 单文件 exe（需要 PyInstaller）
```

约定：只用标准库；判定逻辑保持纯函数；改规则必须同步改
`tests/test_multitenant.py::test_practice_gain_is_stable` 并在 CHANGELOG 说明理由。

目录：

```
server.py            入口：账号会话、静态页、租户绑定、请求分发
auth.py              账号/密码/会话（PBKDF2 + SQLite）
tenant.py            每用户独立状态容器（LRU + 独立锁）
custom_skills.py     每用户自定义技能账本
skill_forge.py       升级机制生成：联网抓资料 + 可选 AI + 本地保守回退
console.py           把控制台切到 UTF-8（Windows 非 UTF-8 代码页用）
engine/              熟练度引擎与规则
  proficiency.py       经验/等级/保持度/事件日志/达标校验
  realistic_rules.py   规则唯一来源：阈值、系数、技能指标、问卷规格
  surveys.py           分技能自评题库与当次问卷（纯函数）
  data_vault.py        导出/导入/滚动备份
  benchmark.py         基准任务复测
  capture_ledger.py    挂机候选时段
  world_state.py       利他层（本机模拟）
  web_app.py           租户化 API 内核
  web_ui.py            前端模板（真源）
ui/index.html        构建产物，由 build_ui.py 生成，勿手改
tests/               多用户、隔离、远控、登录、问卷、自定义技能、每日备份
deploy/              Caddy + docker-compose 公网部署
tools/reset_password.py
```

前端改动的完整流程：改 `engine/web_ui.py` → `python build_ui.py` → 测试里的
`test_ui_is_reproducible_from_product_sources` 会验证二者同源。

---

## 文档与发布

| 想做什么 | 看这里 |
| --- | --- |
| 算分到底怎么算、为什么这样设计 | [docs/architecture.md](docs/architecture.md) |
| 本机 / 局域网 / 公网部署、备份、排障 | [docs/deploy.md](docs/deploy.md) |
| 打 Windows 单文件 exe、发版校验 | [docs/packaging.md](docs/packaging.md) |
| 上传到 GitHub、发 Release | [docs/github.md](docs/github.md) |

---

## 安全与边界（先读再部署）

- 所有练习数据都是**自报**，不验真；证明声明只是字符串，不收图、不核对。
- 没有邮箱验证、没有多因素、没有邮件找回；忘记密码只能本机重置。
- 单进程、单机、SQLite：同一 `data/` 不要起多个进程。
- 反向代理后所有请求来自同一个 IP，登录/注册限流会变成全站配额——公网部署请务必设邀请码，
  并按 [docs/deploy.md](docs/deploy.md) 的清单收紧。
- 漏洞报告方式见 [SECURITY.md](SECURITY.md)。

---

## English

**Guide** (向导) is a self-hosted skill-progress journal for people who are tired of streak
counters. Seven built-in skills plus user-defined ones, eight levels, and a scoring model that
only counts *valid* practice: low quality, repeated easy tasks, and mechanical repetition are
rejected; retention decays with idle time; level-ups require recent quality, at least one
objective measurement, and skill-specific metrics. Everything is self-reported (no verification),
runs on the Python standard library, and stores data in plain files you can export.

**Windows (no Python needed):** [download the single-file exe](https://github.com/CXYM021/guide/releases/latest/download/guide-web-win64.zip)
→ unzip → double-click `guide-web.exe`. The green `Code → Download ZIP` button on the repo
page gives you the *source tree*, which contains no executable: binaries are published in
[Releases](https://github.com/CXYM021/guide/releases) only.

Quick start from source: `python server.py` → open <http://127.0.0.1:8765/> → register → take the
4-question baseline → log your first session.
Deployment, backup and hardening notes live in [docs/deploy.md](docs/deploy.md); the scoring model
is documented in [docs/architecture.md](docs/architecture.md).

MIT licensed.

---

## License

[MIT](LICENSE)

## 今日所得一直是 0 / 连胜不动？

多半是设备时间被改到过未来：存档里留下了比现在晚的记录，「今天」永远到不了那一天。

打开今日页会看到「时间检查」卡片，写着有几条、晚了几天。点「拉回现在」，它只改这些记录的
时间戳，经验、次数、等级一律不动；改完今日所得与连胜会按真实时间重新算。

（时间显示本身按你电脑/手机的本地时区，不再是 UTC。）

## 挂机时切走，时间算不算？

只算「这个页面在前台」的时间。切到别的软件（页面仍可见）继续计时；**最小化、锁屏、
切到别的标签页会暂停**，回到页面时会提示「刚才 N 分钟页面不在前台，没有计入」。
卡片上的「已计前台 X 分 Y 秒」就是真正会进入小票的时长。

人在外面练（跑步、钢琴、线下口语），别靠挂机凑时长——用「补记一次」，走同一套判定门。

