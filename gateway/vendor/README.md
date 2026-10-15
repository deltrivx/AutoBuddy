# vendor/workbuddy_daily.py

来自上游开源项目 [L0NE-6/WorkBuddy-Daily](https://github.com/L0NE-6/WorkBuddy-Daily)（MIT License），
单文件自包含签到脚本，以 vendor 方式内置，不做任何修改（便于跟随上游更新）。

**当前版本：v3.7**（上游 main，2026-10-12 提交）
**升级记录：**
- 2026-10-15 从 v2.1 升级至 v3.7（跨 6 个版本）
  - 备份：`workbuddy_daily.py.bak-v2.1-20261015-195136`
  - 新增：钉钉/青龙通知、任务白黑名单过滤（`--tasks`/`--skip-tasks`）、
    两种连签口径区分（签到连签 / 活跃连签）、Token 健壮性校验、签到诊断增强
  - 移除：开学季活动代码（上游已归档，活动结束）

由 `gateway/wb_daily.py` 以子进程方式调用：传入 `wb_refresh_tokens.json`（由 AutoBuddy
账号池的国内版账号 refresh_token 生成，兼容性已实测：Keycloak RT 可直接走其插件刷新接口，
且刷新后旧 RT 仍有效，只读共用无烧号风险），工作目录隔离，`--query --no-desktop` 运行。

**调用参数兼容性（v3.7 已核验）：** `--no-desktop` / `--query` / `--refresh` /
`--gap` / `--only` 全部保留；`WORKBUDDY_REFRESH_TOKEN` 环境变量与
`wb_refresh_tokens.json` 凭证文件格式均兼容。
