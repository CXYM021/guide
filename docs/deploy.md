# 部署与运维

三种落地方式，按「给谁用」选。

## A. 只本机用

```bash
python server.py
# 或 start.bat / start.sh
```

打开 <http://127.0.0.1:8765/>。默认只监听 `127.0.0.1`，同 WiFi 的别人也连不上。
数据在 `./data`（可用 `PROF_DATA_DIR` 改）。

## B. 同一 WiFi：手机、别人的电脑

```bash
PROF_INVITE_CODE=换成一个不短的码 ./start_lan.sh      # Windows: start_lan.bat（先 set PROF_INVITE_CODE=…）
```

启动横幅会打印真实网卡地址（例如 `http://192.168.1.23:8765/`），手机连同一个 WiFi 打开，
各自注册账号即可。每个账号的数据在 `data/users/<id>/`，互相不可见。

注意：

- 局域网是 **HTTP**，密码与会话明文传输，只建议在可信网络（家里/办公室）里用。
- 防火墙要放行端口。Windows 首次会弹窗，勾选「专用网络」；命令行可加：
  `netsh advfirewall firewall add rule name="guide-web" dir=in action=allow protocol=TCP localport=8765 profile=private`
- `start_lan` 在没设邀请码时会警告。**对外开放就必须设**，否则任何人都能注册。
- 主机必须常开；休眠或换 WiFi 后 IP 可能变，以每次启动打印的地址为准。

PWA 在局域网 HTTP 下不会启用 Service Worker，「添加到主屏」只是个快捷方式，打开仍需主机在线。
真正可安装需要 HTTPS（C 档或 localhost）。

## C. 公网：域名 + HTTPS

```bash
cd deploy
PROF_DOMAIN=skill.example.com \
PROF_INVITE_CODE=你的邀请码 \
docker compose -f docker-compose.public.yml up -d
```

会起两个容器：应用（不对外暴露端口）+ Caddy（自动申请/续期证书，反代到应用）。
打开 `https://skill.example.com/`。

要点：

- DNS 的 A 记录指向服务器，云防火墙放行 80/443。
- 应用侧 `PROF_SECURE_COOKIES=1`、`PROF_ALLOWED_ORIGINS=https://你的域名`（compose 已设）。
- `PROF_INVITE_CODE` 必填；不想让人注册就设 `PROF_MAX_USERS`。
- 数据持久化在 `deploy/data/`，升级镜像不影响账号与进度。
- 想更稳可以在 Caddy 前面再套一层 CDN/WAF；但注意：**反向代理后所有请求来自代理 IP**，
  应用内限流会聚合成全站配额。公网请务必在网关层加限流/人机校验，并设邀请码。

本仓库的 `deploy/` 配置只做了静态核对与请求头级测试（`X-Forwarded-Proto: https` 时
进入公网模式、Cookie 带 `Secure`、不回传内网地址）；没有在真实域名上跑过签证书流程，
首次上线请按下面的清单逐项验收。

## 数据备份与恢复

- 页面「设置 → 数据主权」：导出单账号备份（JSON，含技能、事件、自定义技能），导入会覆盖该账号，
  导入前自动留一份现场备份。
- 整机搬家：停服务，把整个 `data/` 目录复制走即可（含 `app.db` 账号库）。
- 每日滚动备份保留 14 天，位置 `data/users/<id>/backups/`。

恢复：停服务 → 用备份覆盖 `data/` → 启动。

## 升级

```bash
git pull
docker compose build && docker compose up -d     # 或重启 python server.py
```

`data/` 不要删。存档有版本号与向后兼容读取（缺字段取默认），升级不会清档案。

## 忘记密码

```bash
python tools/reset_password.py 用户名
# Docker：
docker compose exec guide python tools/reset_password.py 用户名
```

交互输入新密码；该账号所有旧会话立即失效。没有邮箱找回，这是唯一方式。

## 排障

| 现象 | 处理 |
| --- | --- |
| 端口被占用 | `PROF_PORT=8766 python server.py`，或关掉占用进程 |
| `unable to open database file` | `data/` 权限不对，或同一个 `data/` 被两个进程打开 |
| 手机打不开局域网地址 | 是否同一 WiFi、防火墙是否放行、地址是否用了启动横幅里的那个 |
| 登录一直提示密码错 | 确认没有多余空格；密码不做裁剪，用户名会自动归一化 |
| 反向代理后 403 | `PROF_ALLOWED_ORIGINS` 要填 `https://域名`；或检查代理是否透传 `Host` |
| 页面白屏 | 先看 `/healthz`；再确认 `ui/index.html` 是 `python build_ui.py` 的产物 |

## 上线验收清单

- [ ] 手机用蜂窝网络打开域名，能注册、登录、打卡、刷新后数据还在
- [ ] 第二台设备注册另一个账号，互相看不到对方事件
- [ ] 浏览器开发者工具确认 Cookie 带 `Secure` + `HttpOnly`，页面是 HTTPS
- [ ] 重启容器后账号与进度仍在
- [ ] 手机浏览器菜单「安装应用 / 添加到主屏」，图标与名称正确
- [ ] 邀请码生效（不带码注册被拒）
- [ ] `data/` 已纳入定期备份
