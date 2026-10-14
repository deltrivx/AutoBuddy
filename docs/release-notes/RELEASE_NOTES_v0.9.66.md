> 修复 token 统计页 500：entrypoint 双向软链成环导致 SQLite 打不开数据库。

## 用户反馈

> 下一个 token 统计页面报错 500

## 实测根因

复现 `/api/token-stats` → HTTP 500，容器日志真实堆栈：

```
File "/app/gateway/web_proxy.py", line 6413, in token_stats_api
    stats = get_aggregated_token_stats()
File "/app/gateway/token_store.py", line 91, in init_db
    conn.executescript(...)
sqlite3.OperationalError: unable to open database file
```

现场勘查发现**符号链接成环**：

```
/data/.autobuddy/token_stats.db-wal -> /data/.wb-switch/token_stats.db-wal
/data/.wb-switch/token_stats.db-wal -> /data/.autobuddy/token_stats.db-wal   ← A→B→A
```

`readlink -f` 解析为 UNRESOLVABLE，SQLite 打开时拿到 ELOOP。

**成因**：`entrypoint.sh` 的两轮"双向映射"用 `[ ! -e ]` 判断目标是否已占用。
`-e` **跟随符号链接**，对悬空链接返回 false —— 于是反向那一轮把悬空链接当成
"不存在"，再建一条指回本目录的链接，与既有的正向链接互指成环。

时间线（现场 mtime）：05:05 正向轮建链 → SQLite 干净关闭删掉 wal 实体 →
`.wb-switch` 侧链接悬空 → 12:18 反向轮把它当不存在，又在 `.autobuddy`
建一条指向 `.wb-switch` 的链接 → 死循环。

## 影响面

读路径（`/api/token-stats`）与写路径（token usage flush）**同时**中断。
故障期间日志有 **1839 条** `Error flushing token usage: unable to open database file`
（最后一条 05:02:45Z）——即用量数据在持续丢失。

## 修复

`entrypoint.sh` 三条修正：

1. 只传播**真实实体**：源用 `[ -e ]`（悬空链接直接跳过）；
2. 目标已占用（实体**或**链接都算）→ 跳过，不重复建链；
3. SQLite 边车文件（`-wal` / `-shm` / `-journal`）**永不跨目录链接** ——
   它们是事务副产品，必须与主库同目录，链到别处必然打不开。

线上急救（删 4 个循环/悬空链接，未触碰 7.5MB 主库）后立即恢复：
`usage_logs` 实时写入恢复，最新记录延迟 0 分钟；修复后零新增 flush 错误。

## 测试

新增 `_test_entrypoint_symlink.py`（17 项）：

- 源码契约：`link_if_absent` / `is_sqlite_sidecar`、`[ -e ]` 源判断、
  目标占用含 `-L`、不再有裸 `[ ! -e ]`、三后缀齐全、两轮循环均跳过边车；
- **真实执行**（非纯 AST）：复刻故障现场（悬空链接 + wal/shm 实体）→
  跑修复逻辑 → 验证无环无悬空、普通文件仍双向传播；
- 干净场景验证边车不跨目录、主库仍正常传播；
- 幂等性（二次执行结果一致）；
- 真实故障形态：互指清理后重启，不再重建互指。

全量 **38 个测试文件全绿**。

## 升级说明

重建容器即可。修复后的 entrypoint 会自动避免成环；已生成的循环链接需手工
清理一次（本次线上已处理）。
