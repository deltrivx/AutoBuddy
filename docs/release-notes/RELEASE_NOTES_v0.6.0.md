# AutoBuddy v0.6.0

> 新增侧边栏「每日任务」：内置 WorkBuddy-Daily 签到脚本，账号池国内版账号自动参与，凭据只读共用无烧号风险。

## 变更

### 1. 每日成长任务面板（新入口）
- 左侧边栏新增「每日任务」，与「账号接入」同用 hash 路由范式，两个自定义视图互不干扰。
- 面板三块卡片：调度与执行（启停/间隔/模式 + 立即执行/中止）、参与账号（账号池 cn 账号自动列出 + 补充账号文本框）、执行记录（最近 8 轮 + 实时日志）。

### 2. 内置 WorkBuddy-Daily 脚本托管
- vendor 化上游 [L0NE-6/WorkBuddy-Daily](https://github.com/L0NE-6/WorkBuddy-Daily)（MIT）至 `gateway/vendor/`，不改一行源码，便于跟随上游更新。
- 新服务 `gateway/wb_daily.py` 跑在容器内网 loopback 18093，由 web_proxy 八条 `/api/wb-daily/*` 路由转发，不对外映射端口。
- 执行时把账号池 cn 账号的 refresh_token 生成上游需要的 token 池，子进程运行，30 分钟超时强制回收。

### 3. 凭据安全
- 实测 Keycloak RT 与上游插件续期接口兼容，且续期后旧 RT 仍有效（REUSABLE）：全程只读共用账号池凭据，绝不写回、不覆盖 accounts.json。
- 补充账号仅保存在本地容器（SQLite + json 双持久化），合并写入不清空既有键。

### 4. 部署与依赖
- Dockerfile 补装 `requests`（vendor 脚本唯一第三方依赖）。
- entrypoint 新增服务启动块（`WB_DAILY_ENABLED=1` 默认开，`WB_DAILY_PORT=18093`）。
- 高频轮询接口（status/jobs/health/accounts）接入访问日志降噪。
