# 参与贡献

谢谢你有兴趣。这个项目的目标很小：**让「我在变强」这件事可核对，而不是更热闹**。
提 PR 前请先确认改动符合这个方向——加徽章、加金币、加随机掉落大概率会被拒。

## 本地开发

```bash
git clone <your-fork>
cd proficiency
python -m unittest discover -s tests -p "test_*.py" -v
python server.py        # http://127.0.0.1:8765/
```

Python 3.10+，**不引入第三方运行时依赖**。测试可以只用标准库跑。

## 改代码前

- 判定逻辑（`engine/realistic_rules.py`、`engine/proficiency.py`）保持**纯函数**：
  不读网络、不读全局、不读系统时钟以外的东西（时间由调用方传入）。
- 前端真源是 `engine/web_ui.py`；`ui/index.html` 是 `python build_ui.py` 的产物。
  改了模板必须重建，否则 `test_ui_is_reproducible_from_product_sources` 会失败。
- 新增 API 需要：认证后的租户上下文、输入校验、错误信息可读；不要回传 traceback。
- 新增存档字段要向后兼容：老 JSON 缺字段时取默认值，并写迁移测试。

## 改规则 = 改契约

`tests/test_multitenant.py::test_practice_gain_is_stable` 锁定了「难度3/质量80/他人点评」的
基准增益（25 点）。如果你确实要改规则：

1. 更新这条测试，并在断言里写清新的口径；
2. 在 `CHANGELOG.md` 的「规则变更」下说明：旧值 → 新值、为什么、对已有存档的影响；
3. 如果会让老用户掉级，必须在 PR 描述里写出来。

## 提交

- 小而聚焦的 commit；标题用中文或英文都可以，但要说清「改了什么 + 为什么」。
- 修 bug 请带一条会失败的回归测试（见 `tests/test_login_repeat.py`、`tests/test_unnamed_task.py`）。
- UI 改动请附截图（桌面 + 手机宽度各一张）。

## 发布（维护者）

1. `python -m unittest discover -s tests` 全绿；
2. `python build_ui.py`、`python build_exe.py`；
3. `python smoke_exe.py` 过；
4. 更新 `CHANGELOG.md`，打 tag `vX.Y.Z`，把 `dist/proficiency-web-win64.zip` 传到 Release。
