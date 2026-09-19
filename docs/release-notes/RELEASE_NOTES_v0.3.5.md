# v0.3.5 - 修复「最近使用账号」记录缺失 + 卡片标出最近调用

## 🐞 修复：手动固定 / 指定账号看起来「没生效」

**现象**：把某个账号设为「首选」，或用 `X-WorkBuddy-Account-Id` 指定账号发请求后，`/account-pool/status` 里的 `lastSelectedAccountId` 始终显示成另一个账号，像是配置被忽略了。

**根因**：`select_account()` 有三条选账号路径，但只有 **auto 轮询** 那条会写入 `_ACCOUNT_POOL_RUNTIME["last_selected_id"]`：

```python
if requested_id:
    return by_id.get(str(requested_id))          # ← 不记录
if config.get("mode") == "manual":
    return by_id.get(str(config["manualAccountId"]))  # ← 不记录
...
_ACCOUNT_POOL_RUNTIME["last_selected_id"] = _account_id(acc)   # ← 只有这里记录
```

于是走「请求指定」或「手动固定」时，该字段一直停留在上一次自动分配的结果，造成误判。

**修复**：新增 `_remember_selection(acc, source)`，三条路径统一记录账号与来源：

| `lastSelectedSource` | 含义 |
| :--- | :--- |
| `request` | 由 `X-WorkBuddy-Account-Id` 请求头或 `body.account_id` 指定 |
| `manual` | 由账号池 `mode=manual` + `manualAccountId` 固定 |
| `auto` | 自动轮询分配 |

## 🖱️ WebUI：卡片上标出「最近调用」

账号卡片控制条新增虚线 `最近调用` 标签，直接标出最近一次真实 API 请求用的是哪个账号，不用再去翻接口。

## ✅ 容器内实测结果

| 场景 | 期望 | 实测 |
| :--- | :--- | :--- |
| `mode=manual` 固定到「一杯美式」，连发 2 次 | 两次都是该账号 | 通过 |
| auto + `X-WorkBuddy-Account-Id` 指定某账号 | 命中指定账号，来源 `request` | 通过 |
| 只启用 A 账号，header 指定已停用的 B 账号 | `409` | 通过（`Requested WorkBuddy account is disabled, expired, or unavailable.`） |
| auto 全启用，连发 5 次 | 分摊到多个账号 | 通过（5 次落到 5 个不同账号） |
| 未知账号 ID / 非法 mode 写入配置 | `400` | 通过 |

## 📦 升级方式

```bash
docker pull ghcr.io/deltrivx/workbuddy-switch:latest
```

> Unraid 请通过容器模板重建，不要手工拼接 `docker run`。
