> 修复交互指令取不到值（status/credits/accounts/models 全返回 `?`）。

## 缺陷（2026-10-13 实测）

菜单点击与文本命令都能通，但几个指令返回内容全是 `?`：

```
[AB_STATUS]  OK: AutoBuddy v0.9.50
[AB_CREDITS] OK: 总积分: ?
```

根因：交互指令里的 `_gw_get()` **一律打到网关基址（18091）**，而
`/api/credits/stats`、`/api/gateway-info` 是 **web_proxy 自己（18090）** 的端点
—— 网关侧实测 `/api/credits/stats` 返回 **404**，于是取不到任何字段。

附带问题：`/health` 根本没有 `accounts` 字段（实测为 `null`），
`status` 的账号数也一直显示 `?`。

## 修复

- `_gw_get()` 按路径区分基址：`/api/*` 走本机 18090，网关能力（`/health`、
  `/v1/models`）走 `GATEWAY_BASE_URL`；
- `status` 的账号数改从 `/api/gateway-info` 的 `accounts.total` 取（实测 24）；
- `models` 同时给出自动发现数量（`models_auto_discovered`）。

## 验证（容器内 loopback 实测）

| 指令 | 修复前 | 修复后 |
|---|---|---|
| `status` | 账号池: ? | 账号池: 24 个账号 · 模型 40 个 |
| `credits` | 总积分: ? | 实际数值 |
| `accounts` | 取不到清单 | 账号余额清单 |
| `models` | 仅总数 | 总数 + 自动发现数 |

## 测试

全量 30 个测试文件全绿；Python 语法 + 内嵌 JS `node --check` 通过。
