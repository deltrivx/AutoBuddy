#!/usr/bin/env python3
"""补领（等「异步计分落定」后领奖）回归测试。

调研确认的真实缺口：vendor 脚本在 run_account 末尾**一次性**领走当时已 completed 的任务；
但上游进度是**异步累加**的 —— 脚本跑完时仍是 accepted / in_progress 的任务可能几秒后才达标，
而脚本已经退出，这些任务**永远不会被领**。

补领 `_wait_and_claim_pending()` 建在 `gateway/wb_daily.py` **服务层**（不改 vendor 脚本，
遵守 `vendor/README.md` 的「不做任何修改」）。本测试锁定五条契约。

⚠️ 为什么不 import wb_daily：它依赖 `uvicorn`，本地 / CI 未必装了（实测 `ModuleNotFoundError`）。
与 `_test_log_dedup.py` 的做法一致：解析源码文本校验实现要点，并用等价逻辑验证算法语义。

### 五条契约

1. 函数存在，且接线到 `_execute` 的 `rc == 0` 之后 —— 脚本成功退出才补领；
2. 只处理脚本退出时仍为 accepted / in_progress 的任务码（vendor 领过的都已 claimed，不会重复领）；
3. 轮询上限 5 轮 × 2s（整体约 10 秒，绝不拖长任务）；
4. AT 来源优先级：优先读 vendor 运行期刷新写回的 token 文件，读不到才回退旧 AT（旧 AT 会 401）；
5. 任何异常都不影响主流程 —— 只记日志，返回已领数。
"""


import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SRC = (ROOT / "gateway" / "wb_daily.py").read_text(encoding="utf-8")

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


# ---------------------------------------------------------------------------
# 1. 函数存在 + 接线位置
# ---------------------------------------------------------------------------
print("[1] 函数存在并接线到 _execute 的 rc==0 之后")

check("定义了 _wait_and_claim_pending",
      "async def _wait_and_claim_pending(job: DailyJob, store: dict) -> int:" in SRC)

# 定位 _execute 的函数体，确认调用点在 rc == 0 分支内
m_exec = re.search(r"^async def _execute\(job: DailyJob\):.*?(?=^async def |\Z)", SRC, re.S | re.M)
body = m_exec.group(0) if m_exec else ""
check("_execute 内调用了补领",
      "_wait_and_claim_pending(job, store)" in body)
check("补领仅在 rc == 0 之后触发",
      bool(re.search(r"if rc == 0:.*?_wait_and_claim_pending", body, re.S)))
check("失败分支不补领（rc != 0 无调用）",
      len(re.findall(r"_wait_and_claim_pending", body)) == 1)

# ---------------------------------------------------------------------------
# 2. 只处理仍未达标的任务码
# ---------------------------------------------------------------------------
print("\n[2] 只处理脚本退出时仍 accepted / in_progress 的任务码")

check("筛选 pending 条件含 accepted / in_progress",
      't.get("accept_status") in ("accepted", "in_progress")' in SRC)
check("达标判定为 completed / claimed",
      'in ("completed", "claimed")' in SRC)
check("领过的会移出待办（不重复领）",
      "pending = [c for c in pending if c not in ready]" in SRC)
check("全部达标即提前结束",
      "if not pending:" in SRC and "break" in SRC)

# ---------------------------------------------------------------------------
# 3. 轮询上限
# ---------------------------------------------------------------------------
print("\n[3] 轮询上限 5 轮 × 2s")

check("最多 5 轮", "for _round in range(5):" in SRC)
check("每轮间隔 2 秒", "await asyncio.sleep(2)" in SRC)

# ---------------------------------------------------------------------------
# 4. AT 来源优先级
# ---------------------------------------------------------------------------
print("\n[4] AT 来源优先级（刷新后的优先，旧 AT 兜底）")

check("优先读 wb_refresh_tokens.json",
      "WORK_DIR / \"wb_refresh_tokens.json\"" in SRC)
# 先取 refreshed，为空才回退 ent
order = re.search(
    r"r = refreshed\.get\(user\).*?if not at:\s*at = \(ent\.get\(\"access_token\"\)", SRC, re.S)
check("refreshed 取不到才回退旧 AT", bool(order))

# ---------------------------------------------------------------------------
# 5. 异常不影响主流程
# ---------------------------------------------------------------------------
print("\n[5] 任何异常都不影响主流程")

# 整体 try/except，且 except 里只记日志后返回 claimed
check("整体异常被兜住", "整体异常（不影响主流程）" in SRC)
check("异常后仍返回已领数（return claimed）", "return claimed" in SRC)
check("缺少 httpx 时跳过而非崩溃",
      "缺少 httpx，跳过" in SRC)

# ---------------------------------------------------------------------------
# 6. 等价算法验证：pending → ready → 领奖
# ---------------------------------------------------------------------------
print("\n[6] 等价算法验证（不重复领、全部达标即停）")


def simulate(rounds_status):
    """rounds_status: 每轮各任务的 accept_status 列表。返回补领顺序。"""
    pending = [t["task_code"] for t in rounds_status[0]
               if t.get("accept_status") in ("accepted", "in_progress")]
    claimed = []
    for snap in rounds_status[1:]:
        m = {t["task_code"]: t for t in snap}
        ready = [c for c in pending
                 if (m.get(c) or {}).get("accept_status") in ("completed", "claimed")]
        for code in ready:
            claimed.append(code)
        pending = [c for c in pending if c not in ready]
        if not pending:
            break
    return claimed


snaps = [
    # 脚本退出时的快照
    [{"task_code": "a", "accept_status": "accepted"},
     {"task_code": "b", "accept_status": "in_progress"},
     {"task_code": "c", "accept_status": "claimed"}]
    ,
    # 第 1 轮：a 达标
    [{"task_code": "a", "accept_status": "completed"},
     {"task_code": "b", "accept_status": "in_progress"},
     {"task_code": "c", "accept_status": "claimed"}]
    ,
    # 第 2 轮：b 达标
    [{"task_code": "a", "accept_status": "completed"},
     {"task_code": "b", "accept_status": "completed"},
     {"task_code": "c", "accept_status": "claimed"}]
    ,
]

got = simulate(snaps)
check("未达标的不领（c 已 claimed 不重复领）", "c" not in got, f"got {got}")
check("达标即补领（a、b 都被领）", set(got) == {"a", "b"}, f"got {got}")
check("不重复领（每项只出现一次）",
      len(got) == len(set(got)), f"got {got}")

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
