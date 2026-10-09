#!/usr/bin/env python3
"""今日消耗重归因：把后端「今日桶」里的跨天串账归还给昨天。

### 用户报告的缺陷（2026-10-10 01:03）

「今日消耗积分 6,371.68，明显存在问题」——当时北京时间才过 **1.29 小时**。

### 实测根因

后端 daily 桶由**稀疏快照**做差值得出。实测全部 15 个账号的今日基准锚在
**10-09 20:07**，而最新快照在 10-10 00:57，跨度 **4.85 小时**：

| 账号 | 基准快照 | 最新快照 | 间隔 | 剩余差 |
|---|---|---|---|---|
| 全部 15 个 | 10-09 20:07 | 10-10 00:57 | 4.85h | 合计 6371.68 |

即把 10-09 晚间约 3.5 小时（含 22:24 那轮每日任务）的消耗整段算进了 10-10。
交叉验证：当天仅 395 次请求，而往常 3135 次请求才对应 5280.01 —— 差约 10 倍。

后端是闭源引擎，采集改不了；网关侧用**自己的高分辨率账本**（token_stats.db，
逐请求带时间戳）按「午夜前后请求数」把这段消耗拆开，把昨天的部分还回去。

### 本测试怎么验

用 `ast` 从源码抠出三个**纯函数**真实 `exec`（不是字符串匹配），
灌入复现缺陷的真实参数后断言行为；再对 `_reattribute_today_usage`
做端到端验证（monkeypatch 掉文件/DB 依赖）。
"""

import ast
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
SRC = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")

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
# 用 ast 抠出纯函数真实执行（不 import web_proxy：它依赖 httpx/fastapi）
# ---------------------------------------------------------------------------
tree = ast.parse(SRC)
wanted = {"_plan_from_snapshots", "_split_fraction", "_reattribute_today_usage"}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
found = {n.name for n in nodes}
if not wanted.issubset(found):
    print(f"  FAIL 缺少函数: {wanted - found}")
    raise SystemExit(1)

ns = {"time": time, "os": __import__("os"), "Path": Path, "json": __import__("json"),
      "logging": __import__("logging")}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "<extracted>", "exec"), ns)
plan_from_snapshots = ns["_plan_from_snapshots"]
split_fraction = ns["_split_fraction"]
reattribute = ns["_reattribute_today_usage"]

MS = 1000.0


def h(n):
    return n * 3600.0 * MS


print("[1] _plan_from_snapshots：识别出跨天串账的窗口")

# 复现真实场景：午夜 = 10-10 00:00；基准在 10-09 20:07，最新在 10-10 00:57
mid = 1000.0
base_ts = mid - h(3.88)          # 约 10-09 20:07
latest_ts = mid + h(0.95)        # 约 10-10 00:57
snaps = {"acct1": [(base_ts, 9000.0), (latest_ts, 2628.32)]}
plan = plan_from_snapshots(snaps, mid)
check("识别出 1 个账号需要重归因", len(plan) == 1, f"got {len(plan)}")
if plan:
    b, l, d = plan["acct1"]
    check("baseline 是午夜前最后一条", abs(b - base_ts) < 1.0, f"got {b}")
    check("latest 是最新的", abs(l - latest_ts) < 1.0, f"got {l}")
    check("delta = 9000 - 2628.32 = 6371.68", abs(d - 6371.68) < 0.01, f"got {d}")
    check("窗口确实跨了午夜", b < mid < l)

print("\n[2] 不误伤：无串账的账号应被跳过")

check("窗口全在午夜前 → 跳过",
      plan_from_snapshots({"a": [(mid - h(5), 100.0), (mid - h(1), 80.0)]}, mid) == {})
check("配额补充导致 delta<=0 → 跳过",
      plan_from_snapshots({"a": [(mid - h(1), 50.0), (mid + h(1), 90.0)]}, mid) == {})
check("无午夜前基准 → 跳过",
      plan_from_snapshots({"a": [(mid + h(1), 50.0)]}, mid) == {})
check("空输入 → 返回空", plan_from_snapshots({}, mid) == {})

print("\n[3] _split_fraction：按请求数拆分（高分辨率优先）")

# 窗口 4.83h，其中午夜后仅约 0.95h；请求 300 次在午夜前、20 次在午夜后
f = split_fraction(base_ts, latest_ts, mid, 300, 20)
check("按请求数拆分 = 20/320 = 0.0625", abs(f - 0.0625) < 1e-6, f"got {f}")
check("归还昨天 = 6371.68 * (1-0.0625) ≈ 5973.45",
      abs(6371.68 * (1 - f) - 5973.45) < 0.5, f"got {6371.68 * (1 - f)}")

# 无请求数据时退化为按时间占比
f2 = split_fraction(base_ts, latest_ts, mid, 0, 0)
expect_t = (latest_ts - mid) / (latest_ts - base_ts)
check("无请求数据 → 退化为时间占比", abs(f2 - expect_t) < 1e-6, f"got {f2} expect {expect_t}")
check("时间占比约为 0.197", abs(f2 - 0.197) < 0.01, f"got {f2}")
check("比例落在 [0,1]", 0.0 <= f2 <= 1.0)
check("全在午夜后 → 全部归今日", split_fraction(mid, mid + h(2), mid, 0, 10) == 1.0)

print("\n[4] _reattribute_today_usage：端到端守恒与修正")


class FakeTime:
    """把 time 固定到 10-10 01:17，使午夜可预期。

    ⚠️ 必须透出 struct_time：被测代码用 ``time.struct_time(...)`` 构造午夜。
    少了它会在函数里抛 AttributeError，而该函数为了优雅退化会**吞掉异常**
    返回 0.0 —— 症状是「端到端全返回 0」，很容易被误判成生产代码失效。
    这是测试桩的坑，不是生产缺陷。
    """
    struct_time = time.struct_time

    def __init__(self, base_epoch):
        self.base = base_epoch

    def time(self):
        return self.base

    def localtime(self, ts=None):
        return time.localtime(self.base if ts is None else ts)

    def strftime(self, fmt, *a):
        # 必须尊重传入的 struct_time：被测代码用 time.strftime(fmt, localtime(午夜-86400))
        # 求昨日日期。若忽略入参、一律用 self.base，会得到 ymd == tmd，
        # 于是「加回昨日」分支永不命中 —— 症状是守恒断言失败。同样是测试桩的坑。
        return time.strftime(fmt, a[0] if a else self.localtime())

    def mktime(self, st):
        return time.mktime(st)


# 真实时间：2026-10-10 01:17 CST
fixed = time.mktime((2026, 10, 10, 1, 17, 0, 0, 0, -1))
midnight_real = time.mktime((2026, 10, 10, 0, 0, 0, 0, 0, -1))
mid_ms = midnight_real * 1000.0

data = {
    "accounts": [
        {
            "accountId": "acct1",
            "usageToday": 6371.68,
            "usage7Days": 25941.74,
            "usageThisMonth": 27807.18,
            "daily": [
                {"date": "2026-10-09", "usage": 5280.01},
                {"date": "2026-10-10", "usage": 6371.68},
            ],
        }
    ]
}

snaps = {"acct1": [(mid_ms - h(3.88), 9000.0), (mid_ms + h(0.95), 2628.32)]}
rows = {"acct1": [mid_ms - h(3.0), mid_ms - h(2.0), mid_ms + h(0.2)]}

ns["time"] = FakeTime(fixed)
ns["_load_credit_snapshots"] = lambda: snaps
ns["_request_rows_cached"] = lambda a, b: rows
reattribute = ns["_reattribute_today_usage"]

moved = reattribute(data)
acc = data["accounts"][0]
dl = {r["date"]: r["usage"] for r in acc["daily"]}

check("返回了挪走的总量且 > 0", moved > 0, f"got {moved}")
check("今日桶被下调（不再是 6371.68）", acc["usageToday"] < 6371.68,
      f"got {acc['usageToday']}")
check("daily 今日桶同步修正", dl["2026-10-10"] < 6371.68, f"got {dl['2026-10-10']}")
check("昨日桶被加回（> 原 5280.01）", dl["2026-10-09"] > 5280.01,
      f"got {dl['2026-10-09']}")
check("守恒：今日+昨日 总额不变",
      abs((dl["2026-10-09"] + dl["2026-10-10"]) - (5280.01 + 6371.68)) < 0.05,
      f"got {dl['2026-10-09'] + dl['2026-10-10']}")
check("挪走的量 = 昨日增量",
      abs((dl["2026-10-09"] - 5280.01) - moved) < 0.05,
      f"moved={moved} 增量={dl['2026-10-09'] - 5280.01}")

print("\n[5] 优雅退化：依赖缺失/异常时不炸、不改数")

for label, snap_v, row_v in (("快照为空", {}, rows), ("DB 为空", snaps, {})):
    d2 = {"accounts": [{"accountId": "acct1", "usageToday": 6371.68,
                        "daily": [{"date": "2026-10-09", "usage": 5280.01},
                                  {"date": "2026-10-10", "usage": 6371.68}]}]}
    ns["_load_credit_snapshots"] = lambda v=snap_v: v
    ns["_request_rows_cached"] = lambda a, b, v=row_v: v
    try:
        m = reattribute(d2)
        dl2 = {r["date"]: r["usage"] for r in d2["accounts"][0]["daily"]}
        conserved = abs((dl2["2026-10-09"] + dl2["2026-10-10"]) - (5280.01 + 6371.68)) < 0.05
    except Exception as e:
        m, conserved = None, False
        print(f"      异常: {type(e).__name__}: {e}")
    if label == "快照为空":
        # 无快照 ⇒ 无从归因，必须原样不动
        check("快照为空 → 返回 0 且原值不动",
              m == 0.0 and d2["accounts"][0]["usageToday"] == 6371.68, f"m={m}")
    else:
        # 无请求数据 ⇒ 退化为按时间占比归因（不是不归因），但必须守恒且不炸
        check("DB 为空 → 不崩且仍守恒（退化为时间占比）", conserved, f"m={m}")

# 快照读取抛异常
def _boom():
    raise RuntimeError("boom")


ns["_load_credit_snapshots"] = _boom
d3 = {"accounts": [{"accountId": "acct1", "usageToday": 1.0, "daily": []}]}
try:
    check("快照读取异常 → 返回 0 不抛出", reattribute(d3) == 0.0)
except Exception as e:
    check("快照读取异常 → 返回 0 不抛出", False, f"{type(e).__name__}: {e}")

check("accounts 为空 → 返回 0", reattribute({}) == 0.0)
check("accounts 非列表 → 返回 0", reattribute({"accounts": None}) == 0.0)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
