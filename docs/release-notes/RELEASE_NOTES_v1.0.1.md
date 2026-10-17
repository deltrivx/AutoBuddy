> 修复上游 401 英文错误页原样透传，并落地刷新令牌四项防复发改造，应对上游 session 批量重置。

## 一、背景

2026-10-17 一天之内 17/24 个 codebuddy.cn 账号同时需要重新登录。排查结论是**上游 session 被外部批量重置**（JWT 解签铁证：多个账号的 AT 签发时间变成同一秒，RT 从 1088 字符缩到 700），不是本系统定时任务触发——本系统没有批量自动刷新，`_refresh_one_token` 只在人工点「刷新 Token」时被调用。

这次事件暴露了刷新令牌这一环的四个防护空白，本版一次性补齐。

## 二、刷新令牌防复发四件套（`gateway/refresh_guard.py`）

| 防护 | 说明 | 可配参数（环境变量） |
| :--- | :--- | :--- |
| 单飞 + 锁 | 同一账号刷新并发时只允许一次在飞，其余共享结果，避免前端连点打爆上游端点、或让多份响应互相覆盖写坏账号文件 | `AB_RG_FLIGHT_TIMEOUT`（默认 30s） |
| 失败隔离（需人工重登） | refresh 报 `issued before session started` / `is invalid` 时，说明 RT 已在上游失效，重试无意义，标记「需人工重登」，不再自动重试 | — |
| 批量熔断告警 | 记录刷新失败事件时间线，短时间（默认 120s）内大量账号（默认 ≥5）刷新失败时推送一次告警 | `AB_RG_ALERT_WINDOW` / `AB_RG_ALERT_THRESHOLD` / `AB_RG_ALERT_COOLDOWN` |
| 节奏限制 | 单账号刷新设最小间隔（默认 5 分钟）+ 每日上限（默认 3 次/账号/日） | `AB_RG_MIN_INTERVAL` / `AB_RG_DAILY_LIMIT` |

设计要点：

- **状态独立存 `refresh_state.json`**，不污染账号池核心数据结构（账号对象写额外字段会跟着 `_save_accounts` 落到官方那份文件里，增加官方前端解析出错的概率）。
- **与账号级停用（`account_policy`）刻意分开**：停用 = 不参与轮询（路由据此跳号），「需人工重登」= 只是提示——账号可能仍能用（AT 未过期时），只是要人去重新扫码。
- **所有失败都不影响刷新主流程**：本模块只做记录与提示，永不抛出异常。

## 三、账号池卡片「需重新登录」标记

`/api/account-pool` 给每个账号补 `needsRelogin` 字段，前端据此在卡片上提示该账号 RT 已失效需重新扫码，与「停用」状态区分显示。

## 四、上游 401 英文错误页透传修复

上游对失效 token 直接回 openresty/APISIX 网关的英文 401 页面（`401 Authorization Required`），`chat_completions` 里 4 处透传点把它原样转发给客户端，极不协调。现加 `_friendly_upstream_body()` 辅助函数，401 一律换成中文：

```json
{
  "error": {
    "message": "登录已过期，请重新登录即可",
    "type": "invalid_request_error",
    "code": "token_expired"
  }
}
```

其余状态码保持原样。
