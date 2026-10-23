#!/usr/bin/env python3
"""进程中断（SIGKILL/容器重建/OOM）导致的「僵尸 running 记录」收尾。

### 实测根因（2026-10-13 22:00）

手动触发每日任务跑到 18/22 时执行 `rebuild_container`，容器被 SIGKILL。
结果：面板 runs 列表仍是 20 条、最新停在 10-12——**那轮彻底无痕**。

此前 v0.9.58 只修了「进程内异常/中止」的落库（`_guard` 的 finally 里补
`_persist_run`），但 SIGKILL 连 finally 都没机会跑，所以仍然漏。

### 修复

1. `start_job` 先落一条 `running`（started_at/created_at 立即可见）；
2. 新增 `db.reconcile_orphan_wb_daily_runs()`：把「status=running 且
   finished_at IS NULL」的行改判 `failed` + 补 `finished_at`；
3. `lifespan` 启动时调用一次，收尾上一次进程留下的僵尸。

效果：中断的任务在面板里显示为「失败（进程中断）」，而非永不结束。

本测试锁定契约，防止回退。
"""

import ast
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DB = (ROOT / "gateway" / "db.py").read_text(encoding="utf-8")
SRC_WD = (ROOT / "gateway" / "wb_daily.py").read_text(encoding="utf-8")

_ok = 0
_fail = []


def check(name, cond, extra=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra else ""))


print("[1] 源码契约")

check("db.reconcile_orphan_wb_daily_runs 存在",
      "def reconcile_orphan_wb_daily_runs" in SRC_DB)
check("只收尾 running 且未结束的行",
      "WHERE status = 'running' AND finished_at IS NULL" in SRC_DB)
check("改判 failed", "SET status = 'failed'" in SRC_DB)
check("补 finished_at（COALESCE 不覆盖已有值）",
      "finished_at = COALESCE(finished_at, ?)" in SRC_DB)
check("wb_daily 启动调用收尾", "_reconcile_orphan_runs()" in SRC_WD)
check("lifespan 内调用（启动时）",
      "asyncio.create_task(_scheduler_loop())" in SRC_WD
      and "asyncio.create_task(_patrol_loop())" in SRC_WD
      and "_reconcile_orphan_runs()" in SRC_WD)
check("start_job 先落 running",
      "JOBS[job.id] = job\n    # 先落一条 running" in SRC_WD and "_persist_run(job, [])" in SRC_WD)

print("\n[2] 真实 SQLite 行为验证")

# 用真实 db 模块 + 临时库跑一遍，确保 SQL 语法与语义都对
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
os.environ["AB_DB_PATH"] = str(Path(tmp) / "test.db")

import importlib
import gateway.db as db  # noqa: E402

importlib.reload(db)
db.init_db()  # 建表（其他 _test_ 是纯 AST 契约，这个真跑 SQLite）

# 造三种行：僵尸 running / 正常 done / 已结束的 failed
db.save_wb_daily_run(job_id="zombie", mode="manual", status="running",
                     started_at=time.time() - 600, finished_at=None,
                     accounts=18, summary={"detail": "跑到 18/22 被杀"},
                     log=["[22:00] 账号18 完成"])
db.save_wb_daily_run(job_id="done1", mode="scheduled", status="done",
                     started_at=time.time() - 7200, finished_at=time.time() - 7000,
                     accounts=22, summary={"rc": 0})
db.save_wb_daily_run(job_id="fail1", mode="manual", status="failed",
                     started_at=time.time() - 900, finished_at=time.time() - 800,
                     accounts=3, summary={"detail": "x"})

before = {r["job_id"]: r for r in db.list_wb_daily_runs(limit=10)}
check("僵尸行初始为 running 且无 finished_at",
      before["zombie"]["status"] == "running" and before["zombie"]["finished_at"] is None,
      f"got {before['zombie']}")

n = db.reconcile_orphan_wb_daily_runs()
check("收尾返回 1 条", n == 1, f"got {n}")

after = {r["job_id"]: r for r in db.list_wb_daily_runs(limit=10)}
z = after["zombie"]
check("僵尸行改判 failed", z["status"] == "failed", f"got {z['status']}")
check("僵尸行补上 finished_at", z["finished_at"] is not None)
check("finished_at 晚于 started_at", z["finished_at"] > z["started_at"])
check("done 行不受影响", after["done1"]["status"] == "done")
check("failed 行不受影响", after["fail1"]["status"] == "failed")
check("重复调用为 0（幂等）", db.reconcile_orphan_wb_daily_runs() == 0)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
