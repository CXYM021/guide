# 2026-11-21 决策：把「配置 AI」改成普通人走得完的路；技能标准改「申请—审批」

> 触发：用户 2026-11-21 12:21 的 161 字（`session-archive/77f0a0a0/00-human-turns.txt:278`）：
> 「怎么配置 AI 专有名词太多，用户读不懂 → 用大白话；或者能不能开一个申请平台，
> 用户填申请、我们这边终端审批、通过后给他加上技能说明和升级规则。」
>
> 上一轮会话到 v1.0.10 发布为止就结束了，这条需求**没有动过一行代码**。本文件是动手前的决策记录。
>
> **2026-11-21 夜间更新：L1 已实现并打包成 v1.0.11**（`profile_ai.py` + 三个接口 + 设置页
> 面板 + `mode=local` 主路径）。本章第 5 节是当时的设想，实际落地与它的差异记在第 9 节末尾。
> L2 尚未开工。

---

## 0. 这份文件解决什么

1. 把「改文案」和「改流程」的代价摆在一起，避免用文案去糊一个流程问题。
2. 定下分层落地顺序，说明现在做哪一层、哪一层是过度工程。
3. 给出可直接执行的实施清单（文件、函数、接口、数据结构、文案），下一轮照着做。

---

## 1. 代码现状核对（以下都是本轮实际读到的位置）

| 位置 | 事实 | 对方案的影响 |
|---|---|---|
| `skill_forge.py:224 _ai_config()` | 端点/密钥/模型**只从 env 读**，`config` 字典优先 | 界面内配置要落盘一份 config 再传进来 |
| `skill_forge.py:455 forge_skill_spec(..., config=None)` | **已经支持注入 config 字典** | 后端加一个参数即可，不用改生成逻辑 |
| `skill_forge.py:442 ai_status()` | 只读 env，决定界面显示「已配置/未配置」 | 需要加 `config` 参数，否则界面永远显示未配置 |
| `skill_forge.py:469-471` | 三项缺一 → `local_spec`，写明 `ai_used=false` 与原因 | 「不配也能加技能」已经是真的，文案可以明说 |
| `skill_forge.py:485-489` | AI 输出逐项校验，越界/字段缺失 → 回退本地模板 | 审批台复用同一条路径，标准不会跑飞 |
| `custom_skills.py:35 build_rule()` | spec → 不可变 `SkillRule`，dataclass 兜数值校验 | **审批通过 = 下发一个 spec**，判定引擎零改动 |
| `custom_skills.py:86-117` | `_load()` **只在构造时跑一次**；`_write()` 整份覆盖 | 外部写入（审批台）会被进程内缓存覆盖 → 必须加热重载 |
| `engine/web_app.py:1250-1255` | `/api/skills/custom/add|remove`，加完 `set_extra_rules + register_skill + flush` | 导入技能包必须走**同一个收尾**，不能开第二条判定路径 |
| `engine/web_app.py:469-480 add_custom_skill()` | 唯一入口：forge → registry.add → 注入系统 → flush | 技能包导入照抄这 6 行 |
| `build_ui.py:2` | `ui/index.html` 是**从 `engine/web_ui.py` 的 HTML_PAGE 生成**的 | 改文案只改 `web_ui.py`，再跑 `python build_ui.py` |
| `config.example.json` + `README.md:136` | 有 cfg 文件机制；但**密钥明确只从 env 读、不写进 config.json** | 界面内配 Key = 密钥要落盘，这是要用户知情的交换 |
| `tenant.py:32` | 数据在 `data_dir/users/<租户>/`，每人一份 `custom_skills.json` | 审批台能定位到人写规则，不需要新表 |
| `start_lan.bat` + `invite_code` + `max_users` | 局域网多用户**已经能用** | 审批台若做成网页，基础设施是现成的 |

结论：**用户的第二条想法（申请→审批→下发规则）在现有架构里成本很低**，因为
「技能 = 一份 spec」这个抽象已经在，审批只是换了一个 spec 的来源。
真正的工作量不在判定引擎，在「申请单怎么传、审批台长什么样、规则什么时候生效」。

---

## 2. 问题不在名词，在终点

现在用户要走完的路：

1. 看懂 `PROF_AI_ENDPOINT` 是什么（OpenAI 兼容、chat/completions）→ 卡
2. 知道去哪里申请一个 Key → 卡（这一步根本没有「在软件里」的入口）
3. 打开 PowerShell 设三个环境变量 → 卡（一半人卡在没开过 PowerShell）
4. 在同一个窗口重新启动 exe → 卡

**改文案只能推到第 1 步。** 第 2 步是死路：一个只想记钢琴练习的人，不会为了加一个技能
去注册开发者账号。所以「大白话」的必要性成立，但它是**减痛**，不是**通路**。

反过来看，用户真正想要的不是「AI」，而是：

> 「我加的这个『钢琴』，凭什么说我现在是 LV3？」

这句话的答案有三种来源，产品应该让用户**选来源**，而不是逼他配一个模型：

| 来源 | 谁提供 | 成本 | 可信度 |
|---|---|---|---|
| 通用起步标准 | 软件本地模板，1 秒出 | 0 | 低（自建标准，标 `ai_used=false`） |
| 公开资料折算 | 联网抓 + 可选 AI 折算 | 要 Key | 中（有出处，但不是官方认证） |
| 审核过的标准 | 申请单 → 人工审批 → 技能包 | 要等人 | 高（有人看过、出处写实） |

**产品语义从「配置 AI」翻成「选标准来源」**，是这个需求真正的解。配置 AI 退化成
「来源 2 的一个燃料选项」，位置从「前置条件」变成「可选开关」。

---

## 3. 三条路线与代价

| | L1 大白话 + 界面内配置 | L2 申请单 / 技能包（离线） | L3 局域网审批台 | L4 公网平台 |
|---|---|---|---|---|
| 改多少 | 2 个文件 + 后端 1 个参数 | 新增 2 模块 + 3 接口 + 2 处 UI | L2 + 管理页 + 队列 + 热重载 | 一个独立服务 |
| 需要什么 | 无 | 无（文件靠微信/邮件传） | 一台常开的机器 + 局域网 | 域名/备案/HTTPS/运维 |
| 离线承诺 | 不变 | 不变 | 变成「局域网内可见」 | 破坏 |
| 用户做什么 | 粘一个 Key，或什么都不做 | 导出申请 → 发给你 → 导入回执 | 点申请、等结果 | 注册账号 |
| 你得到什么 | 用户不再卡在配置 | 标准有出处、有人把关 | 实时审批、集中管理 | 规模化 |
| 主要风险 | 密钥落盘；文案写了没人看 | 传递靠人，慢；离线验签挡不住改 | registry 缓存冲突；口令管理 | 成本、合规、被滥用 |

**L4 现在不做**：它要的是账号体系、内容审核、滥用防护、备案和一台永不停的服务器，
产品价值却是「多了一个后台」——与铁律 9（数据在自己手里）直接冲突。

**L3 现在也不做**：给一个还没有真实用户的大厅装门禁。等同时在线 ≥3 人再上。

---

## 4. 决定

1. **v1.0.11 做 L1**：设置页里直接配 AI（四家预设 + 一个输入框），文案全换大白话，
   「不配也能加技能」提到主路径。不新增服务器、不改变判定。
2. **v1.0.12 做 L2 的「申请单」半边**：用户能导出 `技能申请.greq`，你能在**命令行终端**
   审（用户原话就是「我们这边有一个终端进行审批」，那就真的是终端，不做 Web 管理台）。
   审完输出 `技能包.gskill`，用户导入即生效。
3. **L3 等真实并发**再谈。若哪天要做，基础设施已经齐（多租户 + LAN + 邀请码）。
4. 公网 SaaS：不做。

理由一句话：**L1 让用户走得下去，L2 让标准值得信；两件事各管一头，不存在二选一。**

---

## 5. L1 实施清单（v1.0.11）

### 5.1 后端

1. `server.py` / `frozen_entry.py`：读 `config.json` 时保留 `ai` 段
   （`{"preset","endpoint","model","key"}`），`PROF_CONFIG` 注入路径不变。
2. 新增 `profile_ai.py`（约 80 行）：
   - `load_ai_config(data_dir)` → 合并顺序 **env > `data/ai_secret.json` > `config.json` > 默认**
   - `save_ai_config(data_dir, presets, endpoint, model, key)` → 密钥单独写 `data/ai_secret.json`（`chmod 600` 尽力而为），其余写 `config.json`
   - `test_ai_connection(cfg)` → 发一条 1 token 的 ping，回 `{ok, ms, error_human}`
   - `mask_key(key)` → `sk-…a1b2`
3. `engine/web_app.py:470`：`forge_skill_spec(..., config=load_ai_config(...))`；
   `custom_skills_payload()` 里 `ai_status()` 改成带 config 的版本。
4. 新接口（3 个，全部要登录）：
   - `GET  /api/ai/config` → 预设清单 + 当前配置（密钥只回 `mask_key`）
   - `POST /api/ai/config` → 保存
   - `POST /api/ai/test` → 测试连通性，错误翻成人话
5. **`skill_forge.py` 一行不改**。它已经吃 config 字典。

### 5.2 前端（`engine/web_ui.py`，改完 `python build_ui.py`）

设置页「我的技能」顶部变成三块：

1. 状态条：`标准来源：通用起步标准（本地，1 秒）｜公开资料折算（已配置：deepseek-chat）`
2. 「换一个标准来源」折叠面板：四个预设按钮 + 「我自己粘服务地址」+ Key 输入 + 「测试」+「保存」
3. 主按钮改成两个：
   - **`直接用通用标准添加`**（主按钮，默认路径，不需要任何配置）
   - `用资料折算添加`（次要按钮，未配置时置灰并写一行「需要先配好一个 AI 服务，10 秒」）

### 5.3 预设（都可编辑，端点只存 base，`call_llm` 会自动补 `/chat/completions`）

| 预设 | base | 默认模型 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 自定义 | 用户填 | 用户填 |

> 因为 `call_llm()` 会自动补 `/chat/completions`，用户少写后缀不会失败——这点可以写进文案当承诺。

---

## 6. L2 实施清单（v1.0.12）

### 6.1 两个新模块

`skill_requests.py`（约 160 行）：

```python
FORMAT_REQ = "guide-skill-request";  VERSION = 1
FORMAT_PKG = "guide-skill-package"

build_request(*, tenant_id, name, why, what, metrics, now) -> dict
write_request(path, req) -> Path        # 导出 技能申请-<名字>.greq
read_request(path) -> dict              # 校验格式/版本/字段/上限(20 技能, 1-5 指标)
build_package(*, request, spec, reviewer, note, now) -> dict
write_package(path, pkg) -> Path        # 导出 技能包-<名字>.gskill
read_package(path) -> dict              # 校验 checksum + 8 条等级描述 + 指标字段
checksum(fields) -> str                 # sha256(规范化 JSON, sort_keys=True)
```

`approve_console.py`（约 120 行，**命令行一问一答，不做 Web 页**）：

```
python approve_console.py inbox/技能申请-钢琴.greq
```

流程：显示申请人填的内容 → 用 `forge_skill_spec(..., config=你的)` 生成草稿 →
逐条显示 8 条等级描述让你改（回车保留）→ 填审核人 + 备注 → 输出 `outbox/技能包-钢琴.gskill`。

### 6.2 用户侧两个接口 + 两个按钮

- `POST /api/skills/request/export`：把「名字/为什么练/练什么/1–5 个指标」打包成申请单文件
- `POST /api/skills/package/import`：读技能包 → `read_package()` 校验 → **照抄 `add_custom_skill()` 的收尾 6 行** → 立即生效

### 6.3 一条硬约束

判定路径**只能有一条**。导入技能包和界面加技能在 `registry.add → set_extra_rules →
register_skill → flush` 之后必须完全一致；分叉的只能是「spec 从哪来」。
否则会出现「导入的技能等级算得不一样」这类找不着的问题。

### 6.4 关于「校验码」

离线分发的 exe 里内置任何 secret，都能被扒出来，所以校验码**不是防伪**，只做三件事：
防传输损坏、防手改字段、留审核记录。谁都能自己加技能，伪造技能包对他没有收益——
这一点要写进文档，别把它讲成安全机制。

---

## 7. 坑与铁律对齐

| 坑 | 具体表现 | 处置 |
|---|---|---|
| **registry 进程内缓存** | 审批台直接改 `users/x/custom_skills.json`，用户端 `_write()` 一刷新就覆盖回去 | `custom_skills.py:112` 前加 `_reload_if_stale()`（比 `st_mtime_ns`，约 12 行），`ids/specs/rules` 读时也各比一次 |
| **密钥落盘** | 现在密钥只在 env，README 明确不写进 config.json | 单独 `data/ai_secret.json` + 界面只回掩码 + 文档写明「想不落盘就用环境变量」 |
| **铁律 8：AI 不许假装** | 导入的技能包不是 AI 生成的 | `provenance` 里区分 `reviewed` / `ai` / `local`，界面照旧显示 `ai_used` 与原因 |
| **铁律 6：不公开全部判定规则** | 技能包会带 8 条等级描述 | 只带「人看得懂的等级说明」，不带阈值表与系数；规则页展示口径不变 |
| **铁律 9：数据主权** | 申请单里有用户自述 | 申请单只由用户主动导出，产品不自动上传；导出备份要包含新文件类型 |
| **铁律 5：晋升题不能重复刷** | 技能包若带题库就有刷题风险 | 技能包**不带题**，只带等级描述；题目仍走既有 `surveys.py` |

---

## 8. 文案（可直接替换 `engine/web_ui.py` 里 `怎么配置 AI` 那一段）

### 8.1 状态条

```
标准来源：通用起步标准（本地生成，1 秒，不联网）
          ↑ 想换成「有出处的标准」？点右边
```

配置后：

```
标准来源：公开资料折算 · deepseek-chat（已就绪）
          密钥存在这台电脑上，不会上传；练习打分始终本地算，不联网。
```

### 8.2 折叠面板正文

```
这一步是选配。不配，也能加技能。

· 不配会怎样：按你自己填的指标，给一套「通用起步标准」。1 秒出，不联网，
  界面上会明写「本地模板·未用过 AI」，不冒充有人审过。
· 配了会怎样：先上网查公开资料（考级大纲、行业标准这类），再让 AI 把资料
  换算成 LV1–LV8 的门槛。出处会写进技能详情。
· 两种情况都一样：平时练习的打分全在本地算，不会每次联网，练习记录不上传。

三步配好（约 3 分钟）

1. 选一个你已经在用的 AI 服务 —— DeepSeek / 通义千问 / Kimi / OpenAI
2. 粘「钥匙」：在这个服务的「控制台 → API Keys」里生成一串 sk- 开头的字符，复制过来
3. 点「测试一下」，通过就保存

钥匙只存在这台电脑（data/ai_secret.json），界面上只显示后 4 位。
如果你不想让钥匙落盘，跳过这里，改用启动前设环境变量（README 第 120 行起）。
```

### 8.3 加技能区块

```
想加的技能不在列表里？

A. 自己加（现在就能，30 秒）
   填名字 + 1–5 个能数出来的指标，例：每分钟打字数、连续正确天数、完整弹完的曲目数。
   系统按通用起步标准生成 LV1–LV8。

B. 申请「有出处」的标准（要等，标准更硬）
   填一张申请表：叫什么、为什么想练、平时怎么练、怎么算过关。
   导出成文件发给管理员，审核通过后回你一个技能包，点导入就生效。
   申请表里只写你愿意写的内容，软件不会自己往外发。
```

---

## 9. 验证清单

L1（v1.0.11，已完成）：

- [x] 单测 **110 条**全过（92 条零回归 + 新增 18 条：env 优先级、单项 env 不劫持、
      落盘与掩码、payload 不含明文钥匙、留空沿用旧钥匙、坏地址报人话、
      `mode=local` 不联网、文件配置真能驱动生成、接口鉴权与 401 翻译）
- [x] exe 冒烟 **25 项**全过（23 项零回归 + 2 项新增：界面内保存钥匙只回掩码、
      重启后钥匙仍落在该账号目录）
- [x] `python build_ui.py` → `tools/check_ui.py` 同源通过；4 份前端脚本 `node --check` 通过
- [x] 静态校验：177 个 `$('id')` 引用全部有对应元素（`access-btn` / `logout-btn`
      是运行时创建的，属预期）
- [x] 渲染校验：用真实 `<style>` + 真实设置页标记截了 420px 窄屏，面板无重叠、
      主按钮/次按钮层级清楚

> 与第 5 节的差异（实际落地时改的）：
> 1. **没有动的**：`skill_forge.ai_status()` 保持只读 env——界面状态改由
>    `profile_ai.public_status()` 算，因为 env 与文件要合并后才能判断「已配置」。
> 2. **多加的**：`forge_skill_spec(mode="local")`。原设想靠前端控制，最后决定让
>    后端也认这个模式，避免「界面说不联网、后端还是发了请求」这类口说无凭。
> 3. **没做的**：预设端点没有真的各连一次（无凭据）。`/api/ai/test` 的错误翻译
>    用本地 mock 覆盖了 401；402/429 走同一张映射表但未逐个实测。

L2（未开工）：

- [ ] 申请单导出 → 改一个字段 → 导入被校验码挡住
- [ ] 技能包导入 → 与界面加技能得到**同一条**规则（逐字段比对）
- [ ] 审批台外部写入后，运行中的实例能看到（验证 `_reload_if_stale`）
- [ ] 导出备份包含 `custom_skills.json` 与新配置文件

---

## 10. 落地记录与诚实清单

- L1 已实现：新增 `profile_ai.py`、`tests/test_ai_config.py`，改 `skill_forge.py`
  （`mode` 参数）、`engine/web_app.py`（3 个接口 + 注入 config）、`engine/web_ui.py`
  （面板与文案）、`README.md`、`build_exe.py`（exe 内置说明）、`smoke_exe.py`、CI。
- 版本 1.0.10 → **1.0.11**；exe 已重建并实跑冒烟。
- **已发布**：commit `4e50436`、tag `v1.0.11`、Release 挂
  `guide-web-win64.zip` 与 `guide-system-1.0.11.zip`，CI 4/4 绿；
  从 `releases/latest/download/` 重新下载的 zip 与本地 sha256 完全一致。
  发布脚本：`dist/make_release_111.py`（tag 已存在时容忍 422，可重复执行核对部分）。
- 四个预设端点没有逐个实连（手上没有可用钥匙）。
- L2 / L3 的并发与缓存问题只写了方案，没测。
- 已知取舍：密钥落盘是这次唯一的「退步」，用「只回掩码 + 单独文件 + env 优先」
  把暴露面压小，但没有做加密（本地加密只是把钥匙挪个位置，挡不住能读文件的人）。

发布命令（需要时执行）：

```bash
E:/git/Git/bin/git.exe add -A
E:/git/Git/bin/git.exe commit -m "feat(ai): 设置页内配 AI；不配也能加技能（v1.0.11）"
E:/git/Git/bin/git.exe tag v1.0.11
E:/git/Git/bin/git.exe push origin HEAD:main --tags
```
