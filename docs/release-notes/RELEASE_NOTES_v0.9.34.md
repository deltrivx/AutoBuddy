# AutoBuddy v0.9.34

> 移除全部 IP 白名单功能，访问校验回归「密钥 + loopback」两条规则。

## 起因

用户 2026-10-07 反馈：

> 方法做复杂了，取消所有白名单功能

v0.9.32 给每个 API 密钥加了一份白名单，v0.9.33 又补了头部调用量的修正 ——
复杂度是我引入的。本版把它们**整体撤销**，并一并移除更早版本就存在的
**全局 IP 白名单（免密钥）**。

## 移除范围

| 位置 | 移除内容 |
| :--- | :--- |
| `gateway/api_keys.py` | `_STATE["whitelist"]`、`_ip_in_whitelist()`、`set_whitelist()`、`_normalize_ip()`、`get_config()` 的 `whitelist/whitelistCount` 输出、`_load_locked()` 的恢复 |
| `gateway/main.py` | `_gateway_auth()` 里的白名单免密钥判定、`PUT /api-keys/whitelist` 路由 |
| `gateway/web_proxy.py` | 设置页「IP 白名单（免密钥）」区块、`PUT /api/api-keys/whitelist` 转发 |
| 测试 | `_test_api_key_whitelist.py`、`_test_ip_whitelist.py`（测的正是被删功能） |

## 访问校验回归简单

现在只剩两条规则，简单可预期：

```
1. 未开启密钥校验     -> 放行
2. loopback（同容器内） -> 放行
3. 否则校验 Authorization / x-api-key
```

## 说明

- **旧的 whitelist 落盘数据不再恢复**：`_load_locked()` 读到该字段直接忽略，
  不会回灌进内存状态，也不会再写回磁盘（避免僵尸配置复活）。
- **账号池白名单（`enabledAccountIds`）不受影响** —— 那是账号启用列表，
  与本功能无关，完整保留。
- **不影响现有密钥**：密钥本身的增删改、调用计数、启用停用全部照旧。

## 兼容性提示

若此前依赖「白名单免密钥」的客户端（例如某台固定内网 IP 的机器不带密钥
调用 `/v1/*`），升级后需要改为携带 API 密钥。

本项目自身的 OpenClaw workbuddy provider 已配置 `apiKey`，不受影响。

## 测试

```
全量 19 个测试文件               ✅ 全绿
五模块 AST                       ✅ 通过
注入 JS node --check             ✅ 通过
```

## 升级说明

重建容器即可，`/data` 数据不受影响。

无新增环境变量。密钥数据与账号池数据全部保留；
仅 `api_keys.json` 里的 `whitelist` 字段会被忽略（不再读写）。
