# 架构与判定模型

这份文档写给想改代码的人：数据怎么流、等级怎么算、哪些不变量不能破。

## 1. 数据流

```
HTTP 请求
  ├─ 静态资源白名单（manifest / 图标 / sw.js）── 无需登录
  ├─ /healthz、/api/access ───────────────────── 无需登录
  └─ Cookie(sid) → AuthStore.user_for_token → user_id
        → TenantManager.get(user_id) 取/懒加载该用户的状态容器
        → web_app.bind_tenant(tenant) 绑定到当前线程的 contextvars
        → 复用原 API 内核（所有全局状态经 _CtxProxy 转到当前租户）
        → 用该租户的 RLock 串行化同一用户的写操作
        → reset_tenant 还原上下文，返回响应
```

写入只有一个入口：`add_practice()`。

```
录入（抽屉 / 挂机结算 / 快捷补记）
  → web_app.submit_practice       校验字段、读问卷、算 now
  → proficiency.add_practice      技能指标门 → 通用判定 → 更新 state
  → evaluate_practice             纯函数：返回 valid/reason/gain/factors
  → 技能系数 → 当次问卷系数 → 悟道卡 buff
  → 追加只增事件 → 原子写盘
  → 基准复测自动观测（不写经验）→ 返回整份 payload
```

## 2. 两层真值

每个技能的状态只有两个独立真值：

- `total_exp`：单调不减，记录「练出来过多少」
- `retention_bps`：0–10000，记录「现在还留得住多少」

```
effective_exp = total_exp * retention_bps // 10000
level         = table.resolve(effective_exp)      # 派生，不单独维护
```

因此「等级掉了」永远是保持度掉了，而不是经验被扣。事件日志里 `delta` 之和恒等于 `total_exp`，
`verify()` 会检查这一点。

## 3. 单次增益

```
raw  = 20 × difficulty × q_bps × f_bps × m_bps × r_bps / 10000^4
gain = max(1, round(raw))
gain = gain × 技能指标系数 / 10000
gain = gain × 当次问卷系数 / 10000
gain = gain × 悟道卡(10000|20000) / 10000
```

| 系数 | 取值 |
| --- | --- |
| 反馈 `f_bps` | 无 5000 · 自检 8000 · 他人点评 10000 · 客观度量 12500 |
| 挑战 `m_bps` | 拉伸 10000 · 维持 5000 · 过低 1500（钟形：明显超纲也会掉） |
| 重复 `r_bps` | 同任务窗口内重复 ×0.2；占位任务名不参与判定 |
| 同日递减 | 同一天同一技能第 n 次 ≈ 1/(1+0.6(n-1)) |
| 间隔加成 | 隔 1–3 天 ×1.15、4–7 天 ×1.05、>14 天 ×0.95 |
| 技能指标 | 10000 + 500 × 达到「良好线」的指标个数 |
| 当次问卷 | 0.80–1.12（四项连乘后夹紧） |

拒绝优先级：质量 < 40 → 无反馈且质量 < 70 → 连续低难度 → 重复任务。被拒绝的录入**零副作用**
（不写经验、不动保持度、不写练习事件），只出现在提示里。

## 4. 等级门槛

经验只决定「够不够得着」，能不能升还要过校验（`_granted_level` 逐级放行，不能跳级）：

| 等级 | 有效次数 | 窗口均质 | 客观度量 | 最低难度 | 最长搁置 |
| --- | --- | --- | --- | --- | --- |
| LV1 | 0 | – | – | 1 | – |
| LV2 | 3 | 50 | 1 次 | 1 | 30 天 |
| LV3 | 8 | 65 | 2 次 | 2 | 14 天 |
| LV4 | 16 | 75 | 2 次 | 3 | 7 天 |
| LV5 | 30 | 82 | 3 次 | 4 | 3 天 |
| LV6 | 50 | 88 | 3 次 | 5 | 1 天 |
| LV7 | 80 | 90 | 3 次 | 5 | 1 天 |
| LV8 | 120 | 92 | 3 次 | 5 | 1 天 |

「窗口均质」用最近 10 次有效练习去掉两端各 2 次后的均值，所以补一次高质量练习就能改善，
不需要和几年前的自己平均。连续 3 次达到本级要求会触发「快通」，允许一次跨过卡住的校验。

被门槛挡住时，进度条显示「已满」并把**全部**缺项一次列出来，而不是只显示第一条。

## 5. 保持度

`DECAY_ANCHORS`（停练天数 → 保持度）：0/1 天 100%、3 天 98%、7 天 95%、14 天 89%、
30 天 78%、60 天 65%、120 天 50%、365 天 35%。下限 35%。

练习会按「有效次数」把保持度往上拉（恢复公式含质量与中断时长），所以回来练是有用的，
但不会瞬间回到从前。停练判定按秒连续计算，不按「日历日」跳变。

## 6. 首登自评与当次问卷

`engine/surveys.py` 是纯函数，没有 IO：

- `baseline_level(answers)`：4 题共 0–10 分，≥10 → LV3，≥5 → LV2，否则 LV1
- `session_factor_bps(answers)`：4 项连乘后夹在 8000–11200（即 0.80–1.12）

起点等级只写 `state.baseline_level`（等级地板），**不写 `total_exp`**；已有练习记录或
练习/拒绝事件的技能拒绝再设起点。两者都会写进事件日志（`kind=admin`，`delta=0`）。

## 7. 存储

```
data/
  app.db                  users(id, username, password_hash, salt, created_at,
                          last_login_at, username_key) + sessions(token_hash, …)
  users/<32位十六进制>/
    skills.json           Snapshot：configs / states / events / streak / tz_offset_seconds
    world.json            利他层
    captures.json         挂机候选时段
    benchmarks.json       基准卡与复测结果
    decisions.json        拍板选择
    settings.json         日历时区、开发者工具开关
    backups/*.zip         每日 / 导入前 / 手动备份
```

用户 ID 是随机串，不是用户名；所有路径由它拼出并做正则校验，杜绝路径穿越。
API 不接受 `user_id` 参数，只从会话解析。

## 8. API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/skills` | 全量 payload（技能、事件、成就、推荐、问卷待办、时钟…） |
| GET | `/api/surveys` | 首登问卷与当次问卷题面 |
| POST | `/api/baseline` | 提交首登自评，设置起点等级 |
| POST | `/api/practice` | 记录一次练习（可带 `survey` 答案） |
| GET/POST | `/api/skills/custom`、`/api/skills/custom/add|remove` | 自定义技能 |
| GET | `/api/rules`、`/api/decisions` | 规则表与已拍板决策 |
| GET/POST | `/api/export`、`/api/import`、`/api/backup(s)` | 数据主权 |
| GET/POST | `/api/settings` | 时区、开发者工具开关 |
| POST | `/api/advance`、`/api/config`、`/api/exp`、`/api/reset` | **开发者工具**，默认 403/400 拦截 |
| GET | `/healthz` | 版本与在线用户数（无需登录） |

## 9. 不变量（改代码前请确认没破坏）

1. `sum(event.delta) == state.total_exp`
2. `state.level == derived_level(state)`（除非有 `baseline_level` 地板，且地板 ≤ 等级）
3. 事件 `seq` 连续、只增不改
4. 被拒绝的练习不产生 `practice` 事件、不改任何统计
5. 同一用户的写请求串行；不同用户互不阻塞
6. 前端 `ui/index.html` 必须等于 `build_ui.build()` 的输出
