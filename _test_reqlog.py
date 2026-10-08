#!/usr/bin/env python3
"""请求级指标（reqlog）回归测试。

参考 workbuddy2api-panel 的 internal/reqlog：只归档请求元数据，不写提示词、响应正文、凭证。

本测试锁定四条契约：

1. 成功 / HTTP 错误分别归档，汇总字段齐全（success / httpError / successRate / avgMs）；
2. **流式耗时不被低估** —— total_ms 在流结束后才记（不是 TTFB），ttfb_ms 单独记录；
3. 有界环形缓冲：recent 不超 RECENT_MAX（不会随时间无限增长）；
4. reset 清空全部（仅测试用）。

⚠️ 为什么第 2 条最关键：流式请求若按 TTFB 记耗时，整条流几十秒的请求会记成 1~2s，
指标完全失真。判据是 total_ms 反映整条流（sleep 后 done 的 total_ms 必须包含 sleep 时长。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT))

_ok = 0
_fail: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra else ""))


import reqlog  # noqa: E402

reqlog.reset()

print("[1] 归档：成功 / HTTP 错误，汇总字段齐全")

rid = reqlog.new_request_id()
reqlog.begin(rid, model="m", account="a", stream=False)
time.sleep(0.05)
reqlog.done(rid, reqlog.OUTCOME_SUCCESS)

rid2 = reqlog.new_request_id()
reqlog.begin(rid2, model="m", account="a", stream=False)
reqlog.done(rid2, reqlog.OUTCOME_HTTP_ERROR)

snap = reqlog.snapshot()
s = snap["summary"]
check("成功 1 次", s["success"] == 1, str(s))
check("HTTP 错误 1 次", s["httpError"] == 1, str(s))
check("总次数 2", s["total"] == 2, str(s))
check("成功率 50%", s["successRate"] == 50.0, str(s))
check("avgMs > 0", s["avgMs"] > 0, str(s))
check("p50Ms / p95Ms 存在", "p50Ms" in s and "p95Ms" in s, str(s))

print("\n[2] 流式耗时不被低估：total_ms 记整条流，不是 TTFB")

rid3 = reqlog.new_request_id()
reqlog.begin(rid3, model="m", account="a", stream=True)
time.sleep(0.3)
reqlog.set_status(rid3, 200, ttfb_ms=1.0)
reqlog.done(rid3, reqlog.OUTCOME_SUCCESS)

snap = reqlog.snapshot()
rec = [r for r in snap["recent"] if r["rid"] == rid3][0]
check("total_ms 包含整条流（>250ms）", rec["total_ms"] > 250, str(rec))
check("ttfb_ms 单独记录（1.0）", rec.get("ttfb_ms") == 1.0, str(rec))
check("stream 标记保留", rec.get("stream") is True, str(rec))

print("\n[3] 有界环形缓冲：recent 不超 RECENT_MAX")

for _ in range(reqlog.RECENT_MAX + 20):
    r = reqlog.new_request_id()
    reqlog.begin(r, model="m", account="a", stream=False)
    reqlog.done(r, reqlog.OUTCOME_SUCCESS)

snap = reqlog.snapshot()
check("recent 条数 <= RECENT_MAX", len(snap["recent"]) <= reqlog.RECENT_MAX,
      str(len(snap["recent"])))
check("累计计数仍完整（不被缓冲截断）", snap["summary"]["total"] > reqlog.RECENT_MAX,
      str(snap["summary"]["total"]))

print("\n[4] reset 清空")

reqlog.reset()
snap = reqlog.snapshot()
check("reset 后 total = 0", snap["summary"]["total"] == 0, str(snap["summary"]))
check("reset 后 recent 为空", len(snap["recent"]) == 0, str(len(snap["recent"])))

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
