> 国际版模型拉取双通道并发合并、独占模型定向放行路由、`/export-accounts` 默认脱敏。

## 一、更新摘要

本版（v1.0.4）主要完善对国际版（CodeBuddy AI）模型的发现与路由支持，并在敏感数据导出安全性上补全了脱敏机制：

1. **模型拉取双通道并发**：网关启动及周期轮询探测 upstream 模型时，并发请求国内版与国际版（`https://www.codebuddy.ai/v3/config`）端点，去重合并后幂等写入 `upstream_models.json`。
2. **国际版出站路由补齐**：`AI_MODEL_BASE_URL_MAP` 补齐国际版独占模型映射（含 `default-model`、`fast-model`、`balanced-model`、`primary-model`、`o4-mini`、`seedance-2.5`、`enhance-1.0`、`nes-1.2`、`nes-1.1`、`gpt-image-2.5-sunburst` 等），确保出站精准路由至 `https://www.codebuddy.ai/v2`。
3. **国际版独占模型定向放行**：当客户端请求国际版独占模型时，自动定向放行池内所有可用的国际版账号（`variant == "ai"`），无需依赖全局白名单，且严格排除国内账号与被停用账号；常规模型调用严格遵循白名单限制，绝不弱化常规白名单。
4. **`/export-accounts` 默认脱敏**：账号备份导出默认剔除 `access_token`、`refresh_token`、`auth_raw`、`profile_raw` 等敏感串行凭据；完整导出需显式传 `?exportSecrets=true`，前端反代 `web_proxy.py` 同步支持 query 参数透传。

## 二、配置与兼容性

- **零配置升级**：向后兼容既有配置，已有账号池白名单与账号策略不受影响。
- **安全性**：导出账号若用于本地备份迁移且需要凭据，可在导出时带上 `?exportSecrets=true`。
