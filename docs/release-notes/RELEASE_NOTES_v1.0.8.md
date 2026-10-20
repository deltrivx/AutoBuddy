> 安全修复：WebUI 全部 54 个管理端点补登录鉴权。此前 `/api/import` 等写操作未登录可直接调用，线上曾被写入 21 条注入测试账号。

## 安全

### 管理端点登录鉴权门

**事件**：2026-10-20 线上账号池出现 21 条此前不存在的账号（54 vs 基准 33）。逐条排查定性：5 条 `pwn-*`（domain 指向 `169.254.169.254` 云元数据端点、`100.100.100.100` 阿里云元数据，伪造 token）+ 16 条注入测试 payload（`cb-test`、路径穿越 `../../..`、URL 编码 `%2e%2e`、`null`/`enc` 变体）——不是程序 bug，是**未授权写入**。

**根因**：WebUI（18090）的 `/api/*` 转发端点除 6 个外全部没有登录校验；网关（18091）的管理端点（`/import`、`/refresh-token`、`/delete`、`/export-accounts`）同样裸奔。`_gateway_auth` 只挂在 `/v1/*` 模型接口上。未登录请求 POST `/api/import` 即可写入任意账号条目。

**修复**：

- 新增 `require_user` FastAPI 依赖（校验 `autobuddy_session` cookie，未登录 401）
- 全部 **54 个管理端点**统一挂载（含 import/delete/refresh-token/toggle/api-keys 全套/wb-daily 全套等）
- 公开白名单仅 7 项：login、logout、status、avatar、favicon、wecom 回调、gateway-info（前端启动探测）
- 已登录的浏览器会话不受影响（fetch 同源自动带 cookie）

**残留数据处理**：注入账号**未删除**（待人工确认后处理）；哥哥自己加的账号一律未动。`a27ec592`（MrH）保持原样。

**回归测试**：`_test_api_auth_gate.py` 5 项断言锁定（白名单边界、高危写端点、api-keys 全套、401 行为），防回退。
