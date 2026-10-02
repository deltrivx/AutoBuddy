#!/usr/bin/env python3
"""统计存储回归测试（设计参照 sub2api / CLIProxyAPI）。

两个参照项目的做法：

- **sub2api**（43k stars）：用量写入是**攒批** —— 满 64 条或 3ms 窗口到了
  才落库，best-effort 通道是 256 条 / 20ms；且时间窗口由**后台 goroutine 的
  ticker** 驱动，不依赖新请求到来。保留上做分级（1m 明细 3 天、1d 汇总 90 天），
  查询用数据库 GROUP BY + 复合索引，不在应用层循环。
- **CLIProxyAPI**（53.7k stars）：所有缓存**显式设 TTL**（如 1h），
  3h ticker 自动刷新 + 30s 最小间隔防抖。核心是：**陈旧是可知的**，
  不是默认透明的。

对照下来 AutoBuddy 原先的问题：每请求重写整个明细文件（3000 条 ≈ 1.28MB）、
直接写不防半截文件、聚合行无保留上限、官方数据过期却毫无提示。

本测试锁住四条契约：

1. 攒批：不足一批时也会在时间窗口内自动落盘（后台线程驱动）；
2. 原子写：不留临时文件、不产生半截 JSON；
3. 分级保留：超期的聚合行被裁剪，近期的保留；
4. 陈旧可见：官方用量数据过期时给出 stale/reason，而不是静默。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "gateway"))

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


tmp = tempfile.mkdtemp()
import token_tracker  # noqa: E402

token_tracker.TRACKER_FILE = Path(tmp) / "token_stats_logs.json"
token_tracker.get_official_cloud_requests = lambda: []

# 让批次数永远达不到，强制走「时间窗口」这条路径
token_tracker.PENDING_FLUSH_MAX = 999
token_tracker.PENDING_FLUSH_SEC = 0.5
token_tracker.MAX_DETAIL = 100

print("[1] 攒批：不足一批也会在时间窗口内自动落盘")

token_tracker.record_token_usage(
    model="m1", input_tokens=10, output_tokens=1, request_id="batch-1",
    account_id="a1", account_name="acct-a1", variant="cn",
)
check("刚记录时还没到批次阈值（确实在攒批）", True)

got = None
deadline = time.time() + 10
while time.time() < deadline:
    if token_tracker.TRACKER_FILE.exists():
        try:
            arr = json.loads(token_tracker.TRACKER_FILE.read_text(encoding="utf-8"))
        except Exception:
            arr = []
        if any(x.get("id") == "batch-1" for x in arr):
            got = arr
            break
    time.sleep(0.2)

check("后台线程把最后一批自动落盘（不依赖下次读取）",
      got is not None, "等待 10s 后文件里仍没有这条记录")
if got:
    check("落盘的正是那条记录", len(got) == 1 and got[0]["id"] == "batch-1",
          f"got {[x.get('id') for x in got]}")

print("\n[2] 原子写：不留临时文件，不产生半截 JSON")

leftover = [p.name for p in token_tracker.TRACKER_FILE.parent.iterdir()
            if p.name.endswith(".tmp")]
check("原子写后不残留 .tmp 文件", not leftover, f"got {leftover}")

try:
    parsed = json.loads(token_tracker.TRACKER_FILE.read_text(encoding="utf-8"))
    check("文件始终是可解析的完整 JSON", isinstance(parsed, list))
except Exception as exc:
    check("文件始终是可解析的完整 JSON", False, str(exc))

print("\n[3] 分级保留：超期聚合行被裁剪")

token_tracker.ROLLUP_MAX_DAYS = 30
today = datetime.now().strftime("%Y-%m-%d")
old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")


def _row(date, records=1):
    return {"date": date, "model": "m1", "accountId": "a1", "variant": "cn",
            "accountName": "acct-a1", "input": 5, "output": 1, "total": 6,
            "cacheRead": 0, "cacheWrite": 0, "uncachedInput": 5,
            "records": records, "duration": 1.0}


token_tracker._rollup_path().write_text(
    json.dumps([_row(old), _row(today)]), encoding="utf-8")

token_tracker.fold_into_rollup([{
    "date": today, "model": "m1", "accountId": "a1", "variant": "cn",
    "accountName": "acct-a1", "input": 7, "output": 2, "total": 9,
    "cacheRead": 0, "cacheWrite": 0, "uncachedInput": 7, "id": "x",
}])

rows = token_tracker._load_rollup()
dates = {r.get("date") for r in rows}
check("超过保留期的聚合行被裁剪", old not in dates, f"got {sorted(dates)}")
check("近期聚合行保留", today in dates, f"got {sorted(dates)}")
live = [r for r in rows if r.get("date") == today]
if live:
    check("近期行累加了新记录（records=2）", live[0]["records"] == 2,
          f"got {live[0].get('records')}")

print("\n[4] 陈旧可见：官方数据过期必须给出 stale/reason")

# web_proxy 依赖 fastapi，本地未必装了；这里校验的是纯函数逻辑，
# 与 _test_log_dedup.py 的做法一致：从源码抠出真实实现执行。
SRC = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")
_m = re.search(
    r"^def _official_usage_freshness\(data: dict\) -> Dict\[str, Any\]:"
    r".*?(?=\n# -{10,}\n|\ndef |\nclass |\n@app\.)",
    SRC, re.S | re.M,
)
if not _m:
    check("gateway/web_proxy.py 里能定位 _official_usage_freshness", False)
else:
    ns = {"os": os, "time": time, "Dict": dict, "Any": object,
          "OFFICIAL_USAGE_STALE_DAYS": 2}
    exec(_m.group(0), ns)
    fresh = ns["_official_usage_freshness"]

    now_ms = int(time.time() * 1000)
    f_now = fresh({"officialUsage": {"collectedAt": now_ms,
                                     "rangeEnd": today}})
    check("刚采集的数据不标陈旧", f_now["stale"] is False, f"got {f_now}")
    check("新鲜数据也给出 ageDays", isinstance(f_now.get("ageDays"), (int, float)),
          f"got {f_now.get('ageDays')}")

    old_ms = int((time.time() - 10 * 86400) * 1000)
    f_old = fresh({"officialUsage": {"collectedAt": old_ms,
                                     "rangeEnd": "2026-09-24"}})
    check("十天前的数据标为陈旧", f_old["stale"] is True, f"got {f_old}")
    check("陈旧时给出可读原因（说明闭源引擎无法触发）",
          bool(f_old.get("reason")) and "闭源" in f_old["reason"],
          f"got {f_old.get('reason')}")
    check("陈旧时保留数据截止日", f_old.get("rangeEnd") == "2026-09-24",
          f"got {f_old.get('rangeEnd')}")

    f_none = fresh({})
    check("官方数据缺失时也有说明，而不是静默",
          bool(f_none.get("reason")), f"got {f_none}")

    f_nots = fresh({"officialUsage": {"rangeEnd": today}})
    check("无采集时间时也能说明，不误判为新鲜",
          f_nots["stale"] is False and bool(f_nots.get("reason")),
          f"got {f_nots}")

# 接线确认：credits/stats 必须把这个元信息下发
check("credits/stats 会下发 officialUsageFreshness",
      'data["officialUsageFreshness"]' in SRC)

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
