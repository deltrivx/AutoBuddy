# AutoBuddy v0.9.27

> 参照 sub2api / CLIProxyAPI 重构统计存储（攒批 + 原子写 + 分级保留），并让官方数据陈旧可见。

## 为什么要改

v0.9.26 修好了「词元统计只剩两天」，但暴露了更根本的问题：
统计每记录一次就要**重写整个明细文件**，窗口写满时是 1.28MB 一轮 I/O。

于是对照两个同类中转项目的成熟做法做了一轮重构：

| 项目 | 规模 | 参考价值 |
| :--- | :--- | :--- |
| [sub2api](https://github.com/Wei-Shaw/sub2api) | 43k★ | 用量攒批写入、分级保留、DB 侧 GROUP BY 聚合 |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | 53.7k★ | 缓存显式 TTL、ticker 自动刷新、防抖、fsnotify 热加载 |

## 1. 写入改为攒批

sub2api 的做法（`usage_log_repo_insert.go`）：

```go
usageLogCreateBatchMaxSize  = 64
usageLogCreateBatchWindow   = 3 * time.Millisecond
usageLogBestEffortBatchMaxSize  = 256
usageLogBestEffortBatchWindow   = 20 * time.Millisecond
```

原先本网关是「读整个 JSON → append → 写整个 JSON」，每请求一次。
现在只入内存队列，满 32 条或 1 秒才真正落盘
（`AB_TOKEN_PENDING_MAX` / `AB_TOKEN_PENDING_SEC` 可调）。

### 两个必须配套的细节

攒批会引入新问题，缺一个就出错：

**① 读侧强制 flush** —— `get_aggregated_token_stats()` 读取前先把队列落盘。
攒批是为了省写入，但「刚发生的调用在统计页看不到」不能接受。

**② 后台线程驱动时间窗口** —— sub2api 的窗口是后台 goroutine 的 ticker，
**不依赖新请求到来**。这里同样必须有：否则最后一批不足 32 条的记录
会一直留在内存，进程被 SIGKILL（容器重建很常见，`atexit` 根本不执行）
就彻底丢了。已补 daemon 线程按周期落盘，并保留 `atexit` 兜底。

## 2. 原子写

原先 `open(w)` 直接写，写到一半进程被杀会留下半截文件，
下次读取整个统计页就炸。改成写临时文件 + `os.replace()`
（同一分区内是原子操作），明细与 rollup 两处都改。

## 3. 聚合行分级保留

sub2api 按粒度分档保留（1m 明细 3 天 → 1d 汇总 90 天），
原则是**越粗的档保留越久** —— 既能回溯长期趋势，又不会让文件无限膨胀。

新增 `_prune_rollup()`，默认保留 90 天（`AB_TOKEN_ROLLUP_DAYS` 可调），
每次折算时顺带裁剪。

## 4. 官方数据陈旧：修不了，就让它可见

CLIProxyAPI 对所有缓存显式设 TTL（如 `AntigravityReasoningReplayCacheTTL = 1h`），
3h ticker 自动刷新 + 30s 最小间隔防抖。核心思想：
**陈旧是可知的，不是默认透明的。**

本项目的 `officialUsage` 由官方闭源引擎 `autobuddy-engine` 采集。
实测直连 `:57890` 调 `credits/stats` 也不触发重写（POST 返回 405、
文件 mtime 不变）—— 该环节在闭源二进制内，**网关侧无法修复**。

既然修不了上游，就必须把陈旧如实暴露。过去页面显示
「最近更新 9-24」却不说原因，用户只能以为是网关坏了。

现在 `/api/credits/stats` 一并下发 `officialUsageFreshness`：

```json
{"collectedAt": 1790179275670,
 "rangeEnd": "2026-09-24",
 "ageDays": 8.6,
 "stale": true,
 "reason": "官方用量数据由闭源底层引擎采集，当前未再刷新：最近一次采集距今约 8.6 天，数据截止 2026-09-24。该采集环节在官方二进制内部，网关侧无法触发重写；本页的今日消耗已改用实时数据校准，不受此影响。"}
```

默认超过 2 天标为陈旧（`AB_OFFICIAL_USAGE_STALE_DAYS` 可调）。

## 测试

新增 `_test_token_storage.py` 16 项，锁定四条契约：

```
攒批：不足一批也会在时间窗口内自动落盘（后台线程驱动）
原子写：不留 .tmp 临时文件、始终是完整 JSON
分级保留：超期聚合行被裁剪，近期保留且能继续累加
陈旧可见：过期给 stale/reason，缺失/无时间戳也各有说明，不静默
```

已有两个测试按攒批后的真实读取路径补了 `flush_pending(force=True)` ——
它们原先直接读文件，攒批后读不到，这正是「读侧必须 flush」这条设计的由来。

```
全量 18 个测试文件               ✅ 全绿
五模块 AST                       ✅ 通过
注入 JS node --check             ✅ 通过
```

## 升级说明

重建容器即可，`/data` 数据不受影响。

新增可选环境变量（都有默认值，无需配置）：

| 变量 | 默认 | 含义 |
| :--- | :--- | :--- |
| `AB_TOKEN_PENDING_MAX` | 32 | 攒够多少条落盘 |
| `AB_TOKEN_PENDING_SEC` | 1.0 | 攒批时间窗口（秒） |
| `AB_TOKEN_ROLLUP_DAYS` | 90 | 聚合行保留天数 |
| `AB_OFFICIAL_USAGE_STALE_DAYS` | 2 | 官方数据超过几天标陈旧 |

无数据库结构变更；现有 `token_stats_logs.json` 与
`token_stats_rollup.json` 会被就地沿用，无需手工迁移。
