> 修复「执行无记录」的最后一条遗漏路径：进程被 SIGKILL/容器重建时，任务不再无痕消失。

## 实测根因

手动触发每日任务跑到 **18/22** 时执行 `rebuild_container`，容器被 SIGKILL。

结果：面板 runs 列表仍是 20 条、最新停在 10-12——**那轮彻底无痕**。

此前 v0.9.58 只修了「进程内异常/中止」的落库（`_guard` 的 finally 里补
`_persist_run`），但 SIGKILL 连 finally 都没机会跑，所以仍然漏。

## 修复

1. `start_job` 先落一条 `running`（`started_at` 立即可见，不再等结束才出现）；
2. 新增 `db.reconcile_orphan_wb_daily_runs()`：把「`status='running'` 且
   `finished_at IS NULL`」的行改判 `failed` + 补 `finished_at`；
3. `lifespan` 启动时调用一次，收尾上一次进程留下的僵尸。

效果：中断的任务在面板里显示为「失败（进程中断）」，而非永不结束——
也不会再出现「health 显示 running、但 runs 列表查无此轮」的分裂状态。

## 测试

新增 `_test_orphan_runs.py`（15 项）：

- 源码契约：函数存在、SQL 条件精确、改判 failed、COALESCE 不覆盖已有
  `finished_at`、启动调用、`start_job` 先落 running；
- **真实 SQLite 行为验证**（非纯 AST）：造三种行（僵尸 running / 正常 done /
  已结束 failed），验证收尾只命中僵尸行、`done`/`failed` 不受影响、
  `finished_at` 晚于 `started_at`、重复调用幂等返回 0。

全量 **35 个测试文件全绿**。

## 升级说明

重建容器即可。`/data` 数据不受影响。
