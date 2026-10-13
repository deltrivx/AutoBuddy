> 三项急迫修复：模型发现双通道全断、官方消耗图表停更、积分感知调度。

## ① 上游模型不能及时更新（发现通道全断）

**根因实证**：模型自动发现有两条通道，**全断了**——

- `_discover_from_token_stats()` 读 `token_stats_logs.json`，但该文件在 v0.9.29
  迁 SQLite 时已删除 → 函数每次返回空列表（容器内实测：文件不存在）
- `_discover_from_official_usage()` 读 `official_usage_cache.json`，该文件 9-23 起停更
- 连带坑：`_catalog_signature()` 缓存签名也盯着已删除的 JSON（mtime 恒 0），
  即使发现函数修好，缓存也永不失效

实测：网关账本（token_stats.db）里已出现 27+ 个模型，`/v1/models` 却停在 9 月清单。

**修复**：发现函数改读 SQLite `usage_logs` 表（`SELECT DISTINCT model`），老 JSON
保留为回退（兼容旧部署）；缓存签名纳入 `token_stats.db`（每笔请求都写入，
天然变化，新模型调用一次即进目录）。

## ② 官方消耗统计图表停更（v0.9.42 漏了三个顶层字段）

**根因实证**（CDP 实测页面）：顶部四项正常，但「官方积分消耗」图表显示
「数据更新于 09/24 00:01」、日序列停在 09/14→09/24。图表读的是
`officialUsage.daily / models / requests`（顶层字段）——v0.9.42 只接管了
`ou.accounts[]`，漏了这三个，图表数据仍是官方停更的旧值。

**修复**：三个顶层字段全部从重建后的账号明细聚合而来，与 accounts 天然同源：
- `ou.daily`：31 天窗口按日聚合（含每日逐模型明细）
- `ou.models`：全池逐模型聚合（credit / requestCount）
- `ou.requests`：新增 `_gateway_recent_requests()` 从网关账本取近 600 条
  （字段对齐官方模板：accountId / accountName / client / credit / model /
  requestId / requestTime）
- `ou.collectedAt` 刷新到当前（「数据更新于」不再停 9-24）

## ③ 积分感知调度（加权最少连接）

**用户需求**：积分多的账号优先多调用；积分下来后让位给积分更高的账号；
多账号并发；动态轮询。

**实现**：选号排序从纯「在飞请求数最少」改为 `inflight / weight` 最小者优先：
- weight ∝ 积分余额（相对池均值归一，下限 0.05 防除零）
- 积分下降 → weight 自动衰减 → 让位给积分更高的账号，无需人工干预
- 未知余额（快照缺失/新账号）→ 中性权重，不惩罚（避免重蹈
  「新账号一次都没被调用」的老坑）
- 同分沿用轮询起点保均匀；并发分摊（inflight）语义不变
- `AB_CREDIT_WEIGHT=0` 可关闭

例：A 余额 3000、B 1000 → A 承接约 3 倍请求；A 打到 500 时 B 反超。
余额与 credit_floor 同源（`credit_usage_snapshots.json`，带 mtime 缓存，
无额外 IO 开销）。

## 测试

- 新增 `_test_model_discovery.py`（9 项）、`_test_credit_weight_sched.py`（16 项，
  含等价算法验证：3:1 承接、衰减反超、并发分摊、新账号中性、防除零、优雅退化）
- `_test_credits_usage_monotonic.py` 扩至 29 项（图表三件套接管契约）
- 全量 27 个测试文件全绿

## 升级说明

重建容器即可。`AB_CREDIT_WEIGHT` 默认开启；设 `0` 退回旧行为。
