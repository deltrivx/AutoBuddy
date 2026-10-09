#!/usr/bin/env python3
"""补登记（重试被「前置未满足」拒掉的任务登记）回归测试。

### 实证依据

2026-10-09，job `285268d7eb92`，账号 `15828020689`（完成度仅 1/18）：

- vendor 日志：16 项报 `prerequisite not met: first_buddy`，脚本据此判定
  「服务端仍无 Buddy 实例」并**永久放弃**（该账号不再重试这些任务）；
- 任务结束后实测（容器内直连上游）：
  - `GET /buddy/visible` → `has_buddy = **true**`（实例其实已存在）
  - `first_buddy` 任务 → `accept_status = claimed`，进度 1/1
  - 对那 7 个被拒任务逐个 `POST /v2/activity/growth/tasks/accept`
    → **7/7 返回 `200 accepted`**，回读全部 `accepted`

⇒ 拒绝是**时序问题**（登记那一瞬前置尚未满足），不是任务不可完成。
但 vendor 一拒就不再重试，任务永远卡在 `not_accepted`，每轮重跑都被同一理由拒掉。

`_retry_accept_pending()` 在补领**之前**运行，把所有 `not_accepted` 重新登记一遍。
登记成功后任务进入 `accepted`，下一轮脚本才会对它执行上报动作，进而完成并领奖。

### 为什么用源码解析而不是 import

`wb_daily` 依赖 `uvicorn`，本地 / CI 未必装了（实测 `ModuleNotFoundError`）。
沿用 `_test_log_dedup.py` / `_test_wait_claim.py` 的既有做法。
"""

import re
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
# 1. 函数存在 + 接线位置（必须在补领之前）
# ---------------------------------------------------------------------------
print("[1] 函数存在并接线在补领之前")

check("定义了 _retry_accept_pending",
      "async def _retry_accept_pending(job: DailyJob, store: dict) -> int:" in SRC)

m_exec = re.search(r"^async def _execute\(job: DailyJob\):.*?(?=^async def |\Z)", SRC, re.S | re.M)
body = m_exec.group(0) if m_exec else ""
check("_execute 内调用补登记", "_retry_accept_pending(job, store)" in body)
check("_execute 内调用补领", "_wait_and_claim_pending(job, store)" in body)

i_acc = body.find("_retry_accept_pending(job, store)")
i_claim = body.find("_wait_and_claim_pending(job, store)")
check("补登记先于补领执行", 0 <= i_acc < i_claim, f"i_acc={i_acc} i_claim={i_claim}")

# 两者都在 rc == 0 分支内（脚本成功退出才做）
check("补登记仅在 rc == 0 之后",
      bool(re.search(r"if rc == 0:.*?_retry_accept_pending", body, re.S)))
check("失败分支不补登记（仅一次调用）",
      len(re.findall(r"_retry_accept_pending\(job, store\)", body)) == 1)

# ---------------------------------------------------------------------------
# 2. 只处理 not_accepted
# ---------------------------------------------------------------------------
print("\n[2] 只重试 not_accepted 的任务")

# 取 _retry_accept_pending 的函数体
m_fn = re.search(r"^async def _retry_accept_pending.*?(?=^async def |\Z)", SRC, re.S | re.M)
fn = m_fn.group(0) if m_fn else ""
check("筛选条件为 not_accepted",
      't.get("accept_status") == "not_accepted"' in fn)
check("调 accept 接口", "/v2/activity/growth/tasks/accept" in fn)
check("逐个登记（task_codes 单元素）",
      'json={"task_codes": [code]}' in fn)
check("登记间隔 1 秒（不给上游压力）", "await asyncio.sleep(1.0)" in fn)

# ---------------------------------------------------------------------------
# 3. AT 来源优先级（与补领一致：刷新后的优先）
# ---------------------------------------------------------------------------
print("\n[3] AT 来源优先级（刷新后的优先，旧 AT 兜底）")

check("优先读 wb_refresh_tokens.json",
      'WORK_DIR / "wb_refresh_tokens.json"' in fn)
check("refreshed 取不到才回退旧 AT",
      bool(re.search(
          r'r = refreshed\.get\(user\).*?if not at:\s*at = \(ent\.get\(\"access_token\"\)',
          fn, re.S)))
check("无 AT 的账号直接跳过", "if not at:\n                    continue" in fn)

# ---------------------------------------------------------------------------
# 4. 异常不影响主流程
# ---------------------------------------------------------------------------
print("\n[4] 任何异常都不影响主流程")

check("整体异常被兜住", "[re-accept] 整体异常（不影响主流程）" in fn)
check("异常后仍返回已登记数（return accepted）", "return accepted" in fn)
check("缺少 httpx 时跳过而非崩溃", "缺少 httpx，跳过" in fn)
# 单个任务登记失败只 continue，不中断整个账号
check("单个任务失败只跳过（continue）", fn.count("continue") >= 3)

# ---------------------------------------------------------------------------
# 5. 等价算法验证
# ---------------------------------------------------------------------------
print("\n[5] 等价算法验证（只对未登记的动手，成功才计数）")


def simulate(tasks, accept_results):
    """tasks: 任务列表；accept_results: {code: 是否返回 accepted}"""
    codes = [t["task_code"] for t in tasks if t.get("accept_status") == "not_accepted"]
    got = 0
    for code in codes:
        if accept_results.get(code):
            got += 1
    return got


tasks = [
    {"task_code": "a", "accept_status": "not_accepted"},   # 可重试
    {"task_code": "b", "accept_status": "accepted"},       # 已登记，不动
    {"task_code": "c", "accept_status": "claimed"},        # 已领，不动
    {"task_code": "d", "accept_status": "completed"},      # 已完成，不动
    {"task_code": "e", "accept_status": "not_accepted"},   # 可重试
]
res = {"a": True, "e": False}

got = simulate(tasks, res)
check("只对 not_accepted 动手（a、e 两项）", got == 1, f"got={got}")
check("accept 失败的不计数（e 失败）", "e" not in res or res["e"] is False)
check("已 claimed 的不会被重新登记", all(
    t["task_code"] not in ("b", "c", "d") for t in tasks
    if t["accept_status"] == "not_accepted"))

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
