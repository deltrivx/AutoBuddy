> 修复「官方已发布新模型但目录看不到」——接入 /v3/config 动态模型目录；补全「手动执行任务」按钮。

## ① 官方新模型不及时出现 — 接入 /v3/config 动态目录

**根因**：既有发现通道全是**事后回溯**——usage 缓存（9-24 停更）与网关 SQLite
账本都只记「账号调用过的模型」。官方新发布的模型没人调过，永远不会出现。

**修复**（参照 workbuddy2api-panel 的 `FetchModels`，口径 1:1 对齐）：

- 启动 5 秒后即拉一次 `https://copilot.tencent.com/v3/config`（IDE UA
  `CodeBuddyIDE/4.12.0`，否则上游 400），之后每 6 小时刷新
  （`AB_V3_REFRESH_HOURS` 可调）；失败保留旧文件，优雅退化。
- 解析 `data.models[]` + `ProductFeaturesConfig.ModelTrialBanner.banners[]`
  试用横幅（panel 实测 hy4-preview-f 只出现在横幅里），剔除非 chat 模型
  （`auto` / `codewise-*` / `hunyuan-image-alpha` / `nes-gf` / `space-bunny`）。
- 结果落盘 `upstream_models.json` 并入目录；缓存签名纳入该文件（文件更新即失效）。

**实测**：容器内直连 `/v3/config` 返回 200，拉到 22 个模型，含新发布的
`kimi-k3-1`、`kimi-k2.8-preview`、`step-5-preview`、`hy4-preview`、`hy3-x`、
`deepseek-v4-flash/pro`、`glm-5.3-flash`——此前这些全不在 `/v1/models` 里。

## ② 每日任务页补「手动执行任务」按钮

**用户反馈**：有手动签到按钮（全部立即签到），但没有手动执行任务按钮。

**修复**：在「账号签到与凭据」卡片的按钮区（与「全部立即签到」并列）新增
**「全部执行任务」**按钮——走 `/api/wb-daily/run` 全量模式
（签到+成长任务+玩法+领奖），启动后自动轮询进度并打日志到「执行进度」区；
已有任务在跑时提示（409 语义）。

## 测试

- `_test_model_discovery.py` 更新：签名契约扩至四文件（含 upstream_models.json）。
- 全量 27 个测试文件全绿；Python 语法 + 内嵌 JS `node --check` 通过。

## 升级说明

重建容器即可。启动约 5 秒后新模型即进 `/v1/models`（无需等待调用回溯）。
新增环境变量 `AB_V3_REFRESH_HOURS`（默认 6）。
