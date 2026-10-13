#!/usr/bin/env python3
"""积分面板消耗统计：累计值不得小于单日值。

### 用户报告的缺陷（2026-10-09）

「近7日消耗和本月消耗比今日消耗还低」——这在数学上不可能：
今日窗口必然被包含在「近 7 日」与「本月」窗口内，累计值一定 >= 单日值。

线上实测（`/api/credits/stats`）：

| 字段 | 顶层 summary（20 账号） | officialUsage.summary（前端实际采用） |
|---|---|---|
| usageToday | 5280.01 | 5280.01 |
| usage7Days | 19573.14 | **3597.49** ❌ |
| usageThisMonth | 21435.50 | **3819.01** ❌ |

根因：`officialUsage` 这条采集通道只覆盖 **6 个账号**，顶层是全量 **20 个账号**，
所以它的**合计类**字段天然偏小。而官方前端优先采用 `officialUsage`
（`status=complete` 即视为可用），于是把全量累计值覆盖成了残缺值。

这是 2026-10-08 修复「今日消耗一直是 0」时留下的**半截尾巴**：
当初只对账了 `usageToday`，并在注释里假设「usage7Days / usageThisMonth 官方是有值的」——
实测证明该假设不成立，子集通道的所有合计字段都不可信。

### 本测试的验证方式

`_reconcile_official_usage` 只依赖 `time`，是纯 dict 变换。这里用 `ast` 从源码里
把该函数体抠出来真实 `exec`（而不是字符串匹配），灌入复现缺陷的真实数据，
再断言输出满足累积单调性。**能捕获回归，不是摆设。**
"""

import ast
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
# 从源码里真实提取该函数并执行（不 import 整个 web_proxy：它依赖 httpx/fastapi）
# ---------------------------------------------------------------------------
tree = ast.parse(SRC)
fn_node = None
_mdl_node = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "_reconcile_official_usage":
        fn_node = node
    elif isinstance(node, ast.FunctionDef) and node.name == "_gateway_models_by_account":
        _mdl_node = node

if fn_node is None:
    print("  FAIL 找不到 _reconcile_official_usage；测试无法进行")
    raise SystemExit(1)

ns = {"time": time, "os": __import__("os"), "Path": Path,
      # 模块级缓存常量：被抽取的函数会引用，缺了会 NameError，
      # 而生产代码的兜底 try 会把它吞掉 —— 症状是「未接管却静默通过」。
      "_MDL_CACHE": {"at": 0.0, "payload": None},
      "_MDL_TTL_SEC": 120.0,
      "_SNAP_CACHE": {"at": 0.0, "payload": None},
      "_SNAP_TTL_SEC": 120.0,
      "_REQ_CACHE": {"at": 0.0, "payload": None},
      "_REQ_TTL_SEC": 60.0}
# _reconcile_official_usage 现在依赖 _gateway_models_by_account（读网关账本聚合
# 逐模型明细）；必须一起抽取，否则运行时 NameError 会被生产代码的兜底 try 吞掉，
# 症状是「未接管但测试不报错」。
body = [n for n in (_mdl_node, fn_node) if n is not None]
mod = ast.Module(body=body, type_ignores=[])
exec(compile(mod, "<extracted>", "exec"), ns)
reconcile = ns["_reconcile_official_usage"]
# 注入桩：不读真实 DB，返回固定的逐模型明细（覆盖当天）
ns["_gateway_models_by_account"] = lambda: {
    "acc1": {time.strftime("%Y-%m-%d"): [
        {"model": "glm-5.2", "requestCount": 2, "credit": 12.03}]}}
# 图表三件套依赖的请求明细函数也一并注入桩（缺了会 NameError 被生产兜底吞掉）
ns["_gateway_recent_requests"] = lambda limit=600: [
    {"accountId": "acc1", "accountName": "n", "client": "gateway",
     "credit": 1.0, "model": "glm-5.2", "requestId": "r1",
     "requestTime": time.strftime("%Y-%m-%d %H:%M:%S")}]


def build_case(top_today=5280.01, top_7d=19573.14, top_month=21435.49999674,
               ou_today=5280.01, ou_7d=3597.489999029998, ou_month=3819.009998879998):
    """构造复现用户缺陷的输入：顶层全量 vs officialUsage 残缺。

    顶层 daily 里有今天那条，用于触发 daily 兜底分支（与线上一致）。
    """
    today = time.strftime("%Y-%m-%d")
    return {
        "summary": {
            "usageToday": top_today,
            "usage7Days": top_7d,
            "usageThisMonth": top_month,
        },
        "accounts": [
            {"accountId": "acc1", "currentRemaining": 1000.0},
        ],
        "officialUsage": {
            "status": "complete",
            "summary": {
                "usageToday": ou_today,
                "usage7Days": ou_7d,
                "usageThisMonth": ou_month,
            },
            "accounts": [
                {"accountId": "acc1", "usageToday": 0.0,
                 "currentRemaining": None, "daily": [{"date": today, "usage": 5280.01}]},
            ],
        },
    }


# ---------------------------------------------------------------------------
# 1. 用户报告的真实场景
# ---------------------------------------------------------------------------
print("[1] 用户报告的真实数据（今日 5280.01 / 7日 19573.14 / 本月 21435.50）")

d = build_case()
reconcile(d, {"acc1"})
ou = d["officialUsage"]["summary"]
t = ou["usageToday"]
check("近7日 >= 今日", ou["usage7Days"] >= t, f"7d={ou['usage7Days']} today={t}")
check("本月 >= 今日", ou["usageThisMonth"] >= t, f"month={ou['usageThisMonth']} today={t}")
check("近7日取到顶层全量值 19573.14", abs(ou["usage7Days"] - 19573.14) < 0.01,
      f"got={ou['usage7Days']}")
check("本月取到顶层全量值 21435.50", abs(ou["usageThisMonth"] - 21435.50) < 0.01,
      f"got={ou['usageThisMonth']}")
check("今日值保持 5280.01（未被误改）", abs(t - 5280.01) < 0.01, f"got={t}")

# ---------------------------------------------------------------------------
# 2. 顶层缺失时不得把累计刷成 0
# ---------------------------------------------------------------------------
print("\n[2] 顶层累计缺失（值为 0 / None）时，不得用 officialUsage 的残缺值覆盖")

d = build_case(top_7d=0, top_month=None)
reconcile(d, {"acc1"})
ou = d["officialUsage"]["summary"]
# 顶层缺失 ⇒ 无法取到全量值，保留 officialUsage 原值 3597.49；
# 但它小于今日 5280.01（子集采集导致的失真），单调护栏会把它拉平到今日值 ——
# 这是**期望行为**：宁可保守，也不能展示「累计 < 单日」这种违反常识的数字。
check("顶层缺失时近7日被单调护栏拉平到今日值",
      abs(ou["usage7Days"] - ou["usageToday"]) < 0.01,
      f"7d={ou['usage7Days']} today={ou['usageToday']}")
check("拉平后仍满足单调（近7日 >= 今日）",
      ou["usage7Days"] >= ou["usageToday"], f"7d={ou['usage7Days']} today={ou['usageToday']}")
check("拉平不等于把值清零", ou["usage7Days"] > 0, f"got={ou['usage7Days']}")

# ---------------------------------------------------------------------------
# 3. officialUsage 今日为 0（原始缺陷）时，daily 兜底仍能算出今日
# ---------------------------------------------------------------------------
print("\n[3] officialUsage 今日为 0（2026-10-08 原始缺陷场景）")

d = build_case(ou_today=0)
reconcile(d, {"acc1"})
ou = d["officialUsage"]["summary"]
check("今日由 daily 兜底算出而非 0", ou["usageToday"] > 0, f"got={ou['usageToday']}")
check("今日取顶层 5280.01", abs(ou["usageToday"] - 5280.01) < 0.01, f"got={ou['usageToday']}")
check("累计仍 >= 今日", ou["usage7Days"] >= ou["usageToday"], f"7d={ou['usage7Days']}")

# ---------------------------------------------------------------------------
# 4. officialUsage 缺失/为空时不炸
# ---------------------------------------------------------------------------
print("\n[4] officialUsage 缺失或为空时安全返回")

for bad in ({}, {"officialUsage": {}}, {"officialUsage": {"summary": {}}},
            {"officialUsage": {"summary": None}}):
    try:
        reconcile(dict(bad), set())
        ok = True
    except Exception as e:
        ok = False
        print(f"      异常: {type(e).__name__}: {e}")
    check(f"不崩溃: {str(bad)[:40]}", ok)

print("\n[5] 账号明细整体接管（2026-10-11：官方采集停更后 ou.accounts 必须由实时数据接管）")

# 复现线上真实场景：ou.accounts 的 daily 停在 9-24，而顶层 daily 滚动到今天
d5 = build_case()
stale_day = "2026-09-24"
today = time.strftime("%Y-%m-%d")
d5["accounts"] = [
    {"accountId": "acc1", "usageToday": 5280.01, "usage7Days": 19573.14,
     "usageThisMonth": 21435.50, "currentRemaining": 1000.0,
     "daily": [{"date": today, "usage": 5280.01}]},
]
d5["officialUsage"]["accounts"] = [
    {"accountId": "acc1", "usageToday": 0.0, "usage7Days": 57.98,
     "usageThisMonth": 279.50, "currentRemaining": None,
     "daily": [{"date": stale_day, "usage": 57.98}]},
]
reconcile(d5, {"acc1"})
ou5 = d5["officialUsage"]
# 按官方模板 schema 重构：ou.accounts 与 top accounts 字段集合一致但不 alias
check("ou.accounts 已按模板重构（非 alias）",
      ou5["accounts"][0] is not d5["accounts"][0],
      "不应直接引用顶层账号对象")
OU_TEMPLATE_FIELDS = {"accountId", "accountName", "daily", "detailTruncated", "error",
                      "fetchedCount", "models", "ok", "reportedTotal", "requestCount",
                      "usage7Days", "usageThisMonth", "usageToday"}
check("包含官方模板全部字段", OU_TEMPLATE_FIELDS.issubset(set(ou5["accounts"][0].keys())),
      f"缺: {OU_TEMPLATE_FIELDS - set(ou5['accounts'][0].keys())}")
check("daily 行含 models 字段（官方模板）",
      "models" in (ou5["accounts"][0]["daily"][-1] or {}),
      f"daily 行字段: {list(ou5['accounts'][0]['daily'][-1].keys())}")
check("接管后 daily 滚动到今天（不再停在 9-24）",
      (ou5["accounts"][0]["daily"][-1]["date"] == today),
      f"末条日期={ou5['accounts'][0]['daily'][-1]['date']}")
check("接管后账号的 7日/本月是实时值",
      ou5["accounts"][0]["usage7Days"] == 19573.14,
      f"got={ou5['accounts'][0]['usage7Days']}")
check("来源标记 source=gateway-realtime", ou5.get("source") == "gateway-realtime")
check("rangeEnd 对齐到今天", ou5.get("rangeEnd") == today, f"got={ou5.get('rangeEnd')}")
check("ok=True / error=None（模板安全值）",
      ou5["accounts"][0].get("ok") is True and ou5["accounts"][0].get("error") is None)

# 顶层图表三件套（2026-10-13 补全：页面「官方积分消耗」图表读的是 ou.daily/models/requests）
check("ou.daily 顶层日序列滚动到今天",
      (ou5.get("daily") or [{}])[-1].get("date") == today,
      f"末条={(ou5.get('daily') or [{}])[-1].get('date')}")
check("ou.daily 末行含 models 字段",
      "models" in ((ou5.get("daily") or [{}])[-1] or {}))
check("ou.models 逐模型聚合已重建",
      isinstance(ou5.get("models"), list)
      and any(m.get("model") == "glm-5.2" for m in ou5.get("models") or []),
      f"got={ou5.get('models')}")
check("ou.requests 明细已重建",
      isinstance(ou5.get("requests"), list) and len(ou5.get("requests") or []) > 0)
check("ou.collectedAt 已刷新（不再停 9-24）",
      isinstance(ou5.get("collectedAt"), (int, float)) and ou5.get("collectedAt", 0) > 1.7e12)

# 顶层 accounts 为空时不得接管（保留官方原始数据）
d5b = build_case()
d5b["accounts"] = []
reconcile(d5b, {"acc1"})
check("顶层为空时不接管（保留官方 ou.accounts）",
      len(d5b["officialUsage"].get("accounts") or []) == 1
      and d5b["officialUsage"].get("source") is None)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
