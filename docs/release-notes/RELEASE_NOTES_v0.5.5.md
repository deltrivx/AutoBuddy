# AutoBuddy v0.5.5

> 代理架构收敛优化、模板全面去代理与描述脱敏修复。

## 变更

### 1. 代理架构精准隔离
- 容器 entrypoint 不再向全局导出 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY，网关服务及国内模型调用直连出网，彻底消除 7890 拒连导致的接口 ConnectTimeout。
- 代理配置仅保留在 WebUI「账号接入」页面，由用户配置 `register_proxy`，专属用于 GitHub 自动化注册流程。默认指向正确的 OpenClash 地址 `http://[IP]:7890`，Clash REST API 校准为 `http://[IP]:9090`。

### 2. Unraid 模板清理与凭据脱敏修复
- 模板彻底移除代理相关环境变量。
- 账号密码默认值与说明恢复为标准 `admin` 与 `admin123`，不再注入脱敏标记，避免覆盖用户自定义凭据。
