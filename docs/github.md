# 发布到 GitHub（让所有人能下载、能部署）

这份仓库已经按「可直接提交」整理好：`.gitignore` 排除数据与产物、
CI 跑测试、LICENSE / SECURITY / CONTRIBUTING / CHANGELOG 齐全、文档在 `docs/`。
下面两条路任选一条，**A 不需要装任何工具**。

---

## A. 网页上传（最省事，适合第一次）

1. 打开 <https://github.com/new>。
2. Repository name 填 `proficiency`（或任意名字），Description 可以写：
   「本地优先的技能熟练度系统：练一次记一次，等级由证据决定，不靠打卡」。
3. 选 **Public**，**不要**勾 Add a README / .gitignore / license（仓库里已有）。
4. 点 **Create repository**。
5. 在新仓库页面点 **uploading an existing file**。
6. 打开本机 `guide-system` 文件夹，全选里面的**内容**（不是文件夹本身），
   拖进浏览器上传框。`.github`、`docs`、`engine` 这些目录会被保留。
7. Commit message 填 `Initial release v1.0.0`，点 **Commit changes**。
8. 等 GitHub 渲染完，检查首页 README 是否正常显示、Actions 里 `tests` 是否跑绿。
9. 打一个版本：右侧 **Releases → Create a new release**，Tag 填 `v1.0.0`，
   标题 `v1.0.0`，说明从 `CHANGELOG.md` 复制。
10. 如果你已经在本机打过 exe 包，把 `dist/guide-web-win64.zip` 拖到 release 的
    **Attach binaries** 区域，再点 **Publish release**。

> 网页上传对单个文件有 25 MB 限制；本仓库源码远小于此。exe 走 Release 附件，不进仓库。

## B. 用 Git 命令行（后续好维护）

先装 Git for Windows（<https://git-scm.com/download/win>），然后：

```bat
cd C:\Users\MLTZ\Documents\deepseek-harness\default-workspace\guide-system
git init
git add .
git commit -m "Initial release v1.0.0"
git branch -M main
git remote add origin https://github.com/<你的用户名>/proficiency.git
git push -u origin main
```

第一次 push 会要求登录：用浏览器弹出的 GitHub 授权，或建一个
**Personal access token**（Settings → Developer settings → Tokens）当密码用。
之后每次改完：

```bat
git add -A
git commit -m "说明这次改了什么"
git push
```

发版：

```bat
git tag v1.0.0
git push --tags
```

再到网页 Releases 里基于这个 tag 建 release、传 zip。

---

## 发布后建议做的 6 件事

1. **About**：填一句描述和 Topics（`self-hosted`、`skill`、`habit`、`python`、`sqlite`、`pwa`）。
2. **Releases**：至少发一个带 zip 的版本，别人才能「下载即用」。
3. **Issues**：开一个模板，问三件事——系统版本、复现步骤、`python -V`。
4. **分支保护**：Settings → Branches，要求 CI 通过才能合并。
5. **SECURITY.md 里的联系方式**：换成你真实能收到的渠道。
6. **免责声明**：README 已经写明「自我报告、不是认证」；如果你要放到公开服务器，
   在 About 或 README 顶部保留这句话，避免被当成能力证书。

## 别人怎么用（可以复制给用户）

- 只想试试：下载 Release 里的 `guide-web-win64.zip`，解压双击 exe。
- 想长期用：`git clone` 后 `python server.py`，或 `docker compose up -d`。
- 想给自己团队/社群用：参考 [deploy.md](deploy.md) 的「局域网」与「公网」两档，
  公网务必设 `PROF_INVITE_CODE`、`PROF_SECURE_COOKIES=1`、`PROF_ALLOWED_ORIGINS`，
  并在反向代理上开 HTTPS。
