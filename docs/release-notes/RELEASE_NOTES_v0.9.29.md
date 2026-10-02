# AutoBuddy v0.9.29

> 用量明细迁到 SQLite（增量幂等写入 + 索引），聚合逻辑与输出契约一行未改。

## 为什么要迁

明细原先是**单个 JSON 文件**，每次记录都要：

```
读整个文件 → append → 写整个文件
```

窗口写满 3000 条时约 **1.28MB 一轮 I/O**，请求一密就把磁盘打满。
v0.9.27 的攒批只降低了**频率**，单次落盘仍是整文件重写，**成本没变**。

sub2api（43k★）的做法是明细落到数据库表，写入为**增量 INSERT** 且幂等
（`ON CONFLICT ... DO NOTHING`），并建复合索引服务聚合查询。

## 关键取舍：只换存储，不改聚合

这是本次最重要的一条设计决定。

聚合逻辑与输出契约（ `daily` / `models` / `projects` / `dailyByModel` /
`requests` / `sessions` / `summary` ）**一行未改**，换的只是
「明细从哪来、往哪写」。

好处：

- 前端**零改动**；
- 现有回归测试继续验证语义，迁移风险可被测试守住；
- 真正的瓶颈在写入，换成 INSERT 后即解决 —— 不做多余的重构。

新增 `gateway/token_store.py`：

| 能力 | 实现 |
| :--- | :--- |
| 写入 | `INSERT OR IGNORE`（等价 sub2api 的 `ON CONFLICT DO NOTHING`） |
| 幂等 | 同 `request_id` 重复写/重放/并发提交都不重复计数 |
| 聚合折算 | UPSERT 累加，避免读-改-写竞态 |
| 并发 | WAL 模式：读写互不阻塞 |

索引对齐 sub2api 的复合索引思路（列顺序 = 查询条件顺序）：

```
idx_usage_logs_date          (date)
idx_usage_logs_model_date    (model, date)
idx_usage_logs_account_date  (account_id, date)
idx_usage_logs_variant_date  (variant, date)
idx_usage_logs_ts            (ts DESC)
```

## 迁移过程中修掉的三处真实缺陷

这三处都是**迁移本身引入的风险**，不是原有问题，逐一修掉：

**① 迁移会把超期旧记录两头落空**

旧 JSON 里必然混有超期记录（线上 3000 条里就有）。若一股脑 INSERT 进
明细表，读取时会被时间窗口排除，而它们又没被折算进聚合 ——
**明细里没有、聚合里也没有，等于静默丢数据**。

现按保留期分流：新鲜的进明细，超期的直接折算进聚合。

**② 兜底折算写错了后端**

队列撑不住时的降级路径固定写 JSON 聚合文件；SQLite 模式下读取侧从
SQLite 读，这批数据落到没人看的地方 —— 同样是静默丢失。
现按 `TOKEN_STORE` 分发到当前后端。

**③ 保留策略每次落盘都扫全表**

明细超期是以**天**为单位的，每秒扫一次纯属浪费。改为按
`_RETENTION_CHECK_SEC`（默认 3600s）节流。

注意：不能只在启动时跑一次 —— 长跑进程跨过午夜后日期就变了，
必须周期性重判。

## 一处会直接崩的导入问题（自查发现）

`gateway/` 没有 `__init__.py`，且容器内以**顶层模块**方式运行
（`sys.path` 含 `/app/gateway`）。

写成硬 `from gateway import token_store` 会 `ModuleNotFoundError`，
**网关一启动就挂**。

现改为两种模式都试，与 `main.py` / `web_proxy.py` 的既有约定一致：

```python
try:
    from gateway import token_store
except ImportError:
    import token_store
```

## 兼容与回滚

- 默认 `sqlite`；设 **`AB_TOKEN_STORE=json`** 即回到旧行为。
- 首次读取时自动把旧 JSON 明细与聚合迁入 SQLite，**幂等**
  （`meta` 标记 + `INSERT OR IGNORE`），线上历史数据不丢。
- 超期旧记录在迁移时会被折算进聚合，不会两头落空。
- 明细默认 7 天（`AB_TOKEN_DETAIL_DAYS`），聚合 90 天
  （`AB_TOKEN_ROLLUP_DAYS`），与 v0.9.28 语义一致。

## 测试

新增 `_test_token_store_sqlite.py` **26 项**：

```
[1] 幂等写入：同 request_id 重复写不重复计数
[2] 超期明细折算进聚合，input/output/records 全部保住
[3] 迁移：旧 JSON 超期记录不两头落空，重复迁移不翻倍
[4] 输出契约与 JSON 后端一致（结构不变）
[5] 默认后端必须是 sqlite，且保留 AB_TOKEN_STORE 回滚开关
```

三个既有测试改为显式钉住 `TOKEN_STORE = "json"` —— 它们断言的是
JSON 文件行为（原子写、`.tmp` 残留、直接读 `TRACKER_FILE`）。
钉住后端既保留回归覆盖，也**顺带守住了可回滚路径**。

```
全量 19 个测试文件               ✅ 全绿
五模块 AST                       ✅ 通过
两种导入模式（顶层 / 包）        ✅ 均通过
```

## 升级说明

重建容器即可，`/data` 数据不受影响。

- 新增数据文件 `/data/.autobuddy/token_stats.db`（WAL 模式下还会有
  `-wal` / `-shm` 两个伴生文件，属正常现象，勿手动删除）。
- 旧 JSON 文件保留在原地不删，迁移完成后不再写入；
  确认运行稳定后可自行清理。
- 无环境变量强制要求；`AB_TOKEN_STORE` 仅在需要回滚时设置。
