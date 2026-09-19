## ❓ 要回答的问题

「账号池里 1 个国内账号 + 4 个国际账号，并发调用时是**真的并行分摊**，还是**只用了其中一个**？」

在 v0.3.7 之前，这个问题**无法从网关侧回答**：

- 网关只有 `lastSelectedAccountId` 这**一个全局值** —— 只能说明「最近一次是谁」，说不了「N 个并发请求分摊到了几个账号」。
- 上游按账号查用量的接口 `/billing/meter/get-user-request-usage`，对全部 5 个账号（`www.workbuddy.ai` / `www.codebuddy.cn`）**一律返回 404**，走不通按账号归因。

所以本版先补上观测能力，再用真并发实测。

## 🛠️ 本次改动

### 1. 选账号流水（网关侧归因）

`_remember_selection()` 现在把每一次选账号落进 `_SELECTION_LOG`（环形，保留最近 200 条），并打一行日志：

```
[pool] auto    -> 一杯美式 (1a3478dc-d6de-4189-9486-682a74a6c93b)
[pool] request -> angelwized@gmail.com (a75e21b0-3ec4-4986-abb9-3c5a769fcd55)
```

`docker logs WorkBuddy-Switch | grep '\[pool\]'` 即可直接核对分摊情况。

### 2. 新接口

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `GET` | `/account-pool/selections?limit=N` | `total` / `distinctAccounts` / `counts`（各账号命中次数）/ `recent`（最近明细） |
| `POST` | `/account-pool/selections/reset` | 清空流水，便于做干净的压测观测 |
| `GET` | `/account-pool/status` | 新增 `selectionCounts` 字段 |

WebUI 侧同步新增 `/api/account-pool/selections` 与 `/api/account-pool/selections/reset` 两条代理路由。

### 3. 修掉一个并发隐患

`next_index` 的「读-改-写」不是原子操作。若选账号逻辑被放进线程池执行，两个并发请求会读到同一 index，**双双落到同一账号** —— 表现正是「并发时其实只用了其中一个」。现在用 `_SELECTION_LOCK` 把「取号 + 递增」串起来。

> 补充说明：当前 `select_account()` 是在 `async def` 路由里同步调用的，本身不会被其它请求打断，所以实测结果本来就是正确的；加锁是为了防止将来把它挪进线程池后出现退化。

### 4. WebUI 展示

账号卡片控制条新增：

- `已调用 N 次` 计数徽章（有调用时高亮）
- `清零统计` 按钮
- 自动分配提示改为「并发请求会分摊到 **N** 个已启用账号」

## ✅ 实测结论

**20 个真并发请求（`model=hy3`），5 个账号全部参与，每个恰好 4 次。**

```
HTTP 状态码分布: {'200': 20}
耗时: 并发墙钟=3.91s  单请求 min=3.01s max=3.91s avg=3.33s

记录总数=20  命中账号数=5
    4 次  angelwized@gmail.com           (ai)
    4 次  一杯美式                        (cn)
    4 次  palmesewooters371@gmail.com    (ai)
    4 次  w769600627@gmail.com           (ai)
    4 次  samailamuhammadk774@gmail.com  (ai)

PASS: 20 个并发请求分摊到了全部 5 个可用账号，确实并行。
```

`docker logs` 里的 `[pool] auto` 序列是严格轮询的 **4 轮完整覆盖**：

```
auto -> angelwized | 一杯美式 | palmesewooters371 | w769600627 | samailamuhammadk774
auto -> angelwized | 一杯美式 | palmesewooters371 | w769600627 | samailamuhammadk774
auto -> angelwized | 一杯美式 | palmesewooters371 | w769600627 | samailamuhammadk774
auto -> angelwized | 一杯美式 | palmesewooters371 | w769600627 | samailamuhammadk774
```

**并发性佐证**：并发墙钟 3.91s ≈ 单请求均值 3.33s。如果是串行执行，20 个请求至少需要 60s 以上。同时 20/20 全部 `HTTP 200`，说明国内账号与国际账号各自走对了上游域名（`copilot.tencent.com` / `codebuddy.ai`）。

## 📌 语义边界（重要）

- **一条对话请求仍由单个账号完成** —— 不会把同一个请求拆到两个账号，否则上下文与计费都会乱。
- **多个同时到达的独立请求会分摊到不同账号** —— 这才是「并行」的含义。
- 想固定只用某一个账号：在该账号卡片点 `[设为首选]`（`mode=manual`）。
- 想临时指定：请求头 `X-WorkBuddy-Account-Id: <id>`，或请求体 `{"account_id": "<id>"}`。

## 📦 升级方式

```bash
docker pull ghcr.io/deltrivx/workbuddy-switch:latest
```

> Unraid 请通过容器模板重建，不要手工拼接 `docker run`。
