#!/usr/bin/env python3
"""词元统计历史回溯回归测试。

背景（用户 2026-10-02 反馈）：「词元统计页面只有最近两天记录」。

根因：明细日志是**滑动窗口**，上限 MAX_DETAIL 条；实测网关每天产生约
2000 条调用，窗口不到两天就写满，最老的记录被直接丢掉 ——
于是页面永远只剩最近一两天，再往前是空白。

明细又不能简单放大：它每请求都要整文件读+写一次，
3000 条已是 1.28MB 一轮 I/O，放大到几万条会把网关拖垮。

修法是**明细保近期、历史转聚合**：滑出窗口的记录按
「天 x 模型 x 账号 x variant」折算成极小的一行存 token_stats_rollup.json，
聚合统计把明细与 rollup 一起算，请求明细仍只列真实调用记录。

本测试锁住五条契约：

1. 滑出窗口的记录**不会被丢**，会进 rollup；
2. 趋势图/汇总把 rollup 一起算进去（不再只剩两天）；
3. 请求明细只含真实调用记录（聚合行不能混进去冒充调用）；
4. 汇总的 records 按真实调用次数计（聚合行代表 N 次，不能只算 1）；
5. rollup 损坏/缺失时统计页不能挂。

直接 import token_tracker —— 它是纯标准库模块（无 fastapi 依赖），
跑的是真实实现，不抄影子代码。

注：``record_token_usage`` 的日期取的是 ``datetime.now()``，造不出历史日期，
所以要模拟「历史记录被挤出窗口」必须先手工播种带日期的明细再落盘，
再调用一次真实的 record 触发折算。
"""
import json
import shutil
import sys
import tempfile
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
ROLLUP = token_tracker._rollup_path()

# 窗口调到很小，好在几条数据的规模内复现「窗口写满」的真实情形
token_tracker.MAX_DETAIL = 3


def _entry(rid, date, model, inp, out, acc="a1", variant="cn"):
    """手工造一条**带历史日期**的明细（record_token_usage 只能写今天）。"""
    return {
        "id": rid, "time": f"{date} 12:00:00", "ts": 1, "timestamp": 1,
        "date": date, "model": model, "input": inp, "output": out,
        "cacheRead": 0, "cacheWrite": 0, "uncachedInput": inp,
        "total": inp + out, "duration": 1.0,
        "accountId": acc, "accountName": f"acct-{acc}",
        "variant": variant, "source": "gateway",
    }


# 播种 3 条历史：old-1/old-2 同一天（会被合并成一行），old-3 另一天
seed = [
    _entry("old-1", "2026-09-01", "m1", 100, 10),
    _entry("old-2", "2026-09-01", "m1", 200, 20),
    _entry("old-3", "2026-09-02", "m2", 300, 30),
]
token_tracker.TRACKER_FILE.write_text(json.dumps(seed), encoding="utf-8")

# 再真实记录 3 条 -> 共 6 条 > MAX_DETAIL(3)，正好把 3 条播种的全挤出去
for i, (model, inp, out) in enumerate([("m1", 400, 40), ("m2", 500, 50), ("m1", 600, 60)], 1):
    token_tracker.record_token_usage(
        model=model, input_tokens=inp, output_tokens=out,
        request_id=f"new-{i}", account_id="a1", account_name="acct-a1",
        variant="cn",
    )

print("[1] 滑出窗口的记录必须折算进历史聚合，而不是被丢掉")

detail = json.loads(token_tracker.TRACKER_FILE.read_text(encoding="utf-8"))
check("明细窗口被限在上限内", len(detail) == 3, f"got {len(detail)}")
check("明细保留的是最新 N 条",
      [x["id"] for x in detail] == ["new-1", "new-2", "new-3"],
      f"got {[x['id'] for x in detail]}")

rollup = token_tracker._load_rollup()
check("rollup 文件已生成", len(rollup) > 0, f"got {len(rollup)}")

r901 = [r for r in rollup if r.get("date") == "2026-09-01"]
if r901:
    check("同天同模型的被挤记录合并成一行", len(r901) == 1, f"got {len(r901)}")
    check("聚合行累记输入 token（100+200）",
          r901[0]["input"] == 300, f"got {r901[0].get('input')}")
    check("聚合行累记输出 token（10+20）",
          r901[0]["output"] == 30, f"got {r901[0].get('output')}")
    check("聚合行记了调用次数 records=2",
          r901[0]["records"] == 2, f"got {r901[0].get('records')}")
else:
    check("最早记录被折进 rollup", False, "rollup 里找不到 2026-09-01")

r902 = [r for r in rollup if r.get("date") == "2026-09-02"]
check("另一天的记录也保住了", len(r902) == 1, f"got {r902}")

print("\n[2] 趋势图/汇总要把历史一起算进去（不再只剩两天）")

stats = token_tracker.get_aggregated_token_stats()
src = stats["sources"][0]
dates = sorted(d["key"] for d in src["daily"])
check("按天覆盖到最早的旧日期 09-01", "2026-09-01" in dates, f"got {dates}")
check("按天覆盖到 09-02", "2026-09-02" in dates, f"got {dates}")
check("天数 >= 3（旧逻辑只会计出窗口内的今天）", len(dates) >= 3, f"got {dates}")

d0901 = next(d for d in src["daily"] if d["key"] == "2026-09-01")
check("该天 input 为两条之和（300）", d0901["input"] == 300, f"got {d0901['input']}")
check("该天 records 计为 2 次真实调用",
      d0901["records"] == 2, f"got {d0901.get('records')}")

print("\n[3] 请求明细只列真实调用记录，聚合行不得混进去")

ids = [r["id"] for r in src["requests"]]
check("明细只含窗口内的真实调用", sorted(ids) == ["new-1", "new-2", "new-3"], f"got {ids}")
check("明细无重复 id", len(ids) == len(set(ids)), f"got {ids}")

print("\n[4] 汇总 records 按真实调用次数计，不是按行数计")

summary = src["summary"]
# 真实调用共 6 次：历史 3 次（old-1,2,3）+ 明细 3 次（new-1,2,3）
check("summary.records = 6（历史 3 + 明细 3）",
      summary["records"] == 6, f"got {summary['records']}")
check("summary.input 汇总含历史（100+200+300+400+500+600=2100）",
      summary["input"] == 2100, f"got {summary['input']}")

m1 = next(m for m in src["models"] if m.get("key") == "m1")
check("按模型桶也含历史部分（100+200+400+600=1300）",
      m1["input"] == 1300, f"got {m1['input']}")

print("\n[5] rollup 损坏/缺失时统计页不能挂")

ROLLUP.write_text("{ this is not json", encoding="utf-8")
try:
    s2 = token_tracker.get_aggregated_token_stats()["sources"][0]
    check("rollup 损坏时不抛异常", True)
    check("损坏时退化为只算明细，输入不为 0",
          s2["summary"]["input"] > 0, f"got {s2['summary']['input']}")
except Exception as exc:
    check("rollup 损坏时不抛异常", False, str(exc))

ROLLUP.unlink(missing_ok=True)
try:
    s3 = token_tracker.get_aggregated_token_stats()["sources"][0]
    check("rollup 不存在时不报错", s3["summary"]["input"] > 0,
          f"got {s3['summary']['input']}")
except Exception as exc:
    check("rollup 不存在时不报错", False, str(exc))

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
