#!/usr/bin/env python3
"""打卡事件时间戳抖动回归测试（2026-10-21 优化方案 ⑥）。

背景：一次批量上报的 N 个事件此前共用同一个 `int(time.time()*1000)`，
在服务端眼里是同一毫秒齐落；chat_request_events 的三个事件更是
写死 now / now+50 / now+100 的固定等差。真人不可能这样操作。

本测试锁定四条契约：

1. 一批事件的时间戳**严格递增**（单调，不允许后者早于前者）；
2. 相邻间隔**不是**固定等差（抖动真的生效）；
3. reportDelay 落在 [0, REPORT_DELAY_MAX_MS) 内且不恒为 0；
4. 事件自带的 timestamp 被尊重（不被 report() 覆盖）——
   这条是踩过的坑：report() 里 env.update(e) 曾把
   chat_request_events 算好的序列整个盖掉。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "gateway" / "vendor"))

_ok = 0
_fail: list = []


def check(name, cond, extra=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra else ""))


# workbuddy_daily.py 是「脚本式」模块：import 时会跑 load_accounts()、
# 解析 sys.argv、建目录等副作用，不能直接 import。故按 _test_* 的既有
# 做法：把需要的两个函数与常量切片出来执行。
src = (ROOT / "gateway" / "vendor" / "workbuddy_daily.py").read_text(encoding="utf-8")

_a = src.index("EVENT_DRIFT_MIN_MS")
_b = src.index("REPORT_DELAY_MAX_MS")
_b = src.index("\n", src.index("# reportDelay 上限", _b))
consts = src[_a:_b]

_c = src.index("def chat_request_events(")
_d = src.index("\n\n\n", _c)
fn_events = src[_c:_d]

_e = src.index("def report(s, uid, nick, events")
_f = src.index("\n\n\n", _e)
fn_report = src[_e:_f]

ns = {
    "time": time,
    "random": __import__("random"),
    "uuid": __import__("uuid"),
    "int": int, "len": len, "max": max, "str": str,
    "EVENT_DRIFT_MIN_MS": 80.0, "EVENT_DRIFT_MAX_MS": 400.0,
    "REPORT_DELAY_MAX_MS": 300,
    # report() 里用 `(base or BASE) + "/v2/report"`，切片没带上 BASE 常量，
    # 不补会在 FakeSession 之前就 NameError，被 try 吞掉变成 return 0。
    "BASE": "https://example.invalid",
}
exec(compile(consts, "consts", "exec"), ns)
exec(compile(fn_events, "events", "exec"), ns)
exec(compile(fn_report, "report", "exec"), ns)

# 依赖：derive_id / UA（chat_request_events 里用到）
ns["derive_id"] = lambda uid, kind: "%s-%s" % (kind, uid)
ns["UA"] = "Mozilla/5.0 test"

chat_request_events = ns["chat_request_events"]
report = ns["report"]

print("== chat_request_events：三事件时间戳 ==")
tss = []
for _ in range(20):
    evs, _rid = chat_request_events("u1", "nick", "conv", "hello", "world")
    tss.append([e["timestamp"] for e in evs])

# 契约 1：严格递增
bad_mono = [t for t in tss if not (t[0] < t[1] < t[2])]
check("三事件时间戳严格递增（20 轮）", not bad_mono, f"反例={bad_mono[:3]}")

# 契约 2：相邻间隔不是固定等差（旧实现恒为 50/100）
gaps = {(t[1] - t[0], t[2] - t[1]) for t in tss}
check("相邻间隔不固定（抖动生效）", len(gaps) > 5, f"仅 {len(gaps)} 种间隔组合")

# 间隔量级合理：单个间隔应在 80~400ms 区间内（递增随机偏移的步长）
all_gaps = [g for t in tss for g in (t[1] - t[0], t[2] - t[1])]
check("单个间隔落在 [80, 400] ms 内",
      all(80 <= g <= 400 for g in all_gaps),
      f"范围=[{min(all_gaps)}, {max(all_gaps)}]")
check("整批事件总跨度 < 1s（不超签到超时）",
      all(t[2] - t[0] < 1000 for t in tss),
      f"最大跨度={max(t[2] - t[0] for t in tss)}ms")

print("\n== report()：reportDelay 抖动 ==")
evs, _ = chat_request_events("u1", "nick", "conv", "hello", "world")
out_env = []
captured = {}


class FakeResp:
    status_code = 200


class FakeSession:
    def post(self, url, json=None, timeout=None, headers=None, verify=None):
        captured["payload"] = json
        return FakeResp()


report(FakeSession(), "u1", "nick", evs)
payload = captured.get("payload") or []
delays = [e.get("reportDelay") for e in payload]

check("reportDelay 不再是恒定的 0", len(set(delays)) > 1, f"got {set(delays)}")
check("reportDelay 落在 [0, 300) 内",
      all(isinstance(d, int) and 0 <= d < 300 for d in delays), f"got {delays}")
check("reportDelay 是 int（协议要求）",
      all(isinstance(d, int) for d in delays), f"got {[type(d).__name__ for d in delays]}")

print("\n== 契约 4：report() 尊重事件自带 timestamp ==")
evs, _ = chat_request_events("u1", "nick", "conv", "hello", "world")
orig_ts = [e["timestamp"] for e in evs]
report(FakeSession(), "u1", "nick", evs)
got_ts = [e["timestamp"] for e in (captured.get("payload") or [])]
check("自带 timestamp 未被覆盖", got_ts == orig_ts,
      f"orig={orig_ts} got={got_ts}")
check("覆盖后仍严格递增", got_ts[0] < got_ts[1] < got_ts[2], f"got {got_ts}")

print("\n== 契约：无 timestamp 的事件由 report() 补抖动 ==")
evs2 = [{"eventCode": "x"}, {"eventCode": "y"}, {"eventCode": "z"}]
report(FakeSession(), "u1", "nick", evs2)
ts2 = [e["timestamp"] for e in (captured.get("payload") or [])]
check("补齐的时间戳严格递增", ts2[0] < ts2[1] < ts2[2], f"got {ts2}")
gaps2 = [ts2[1] - ts2[0], ts2[2] - ts2[1]]
check("补齐的间隔也不固定（不是等差）", gaps2[0] != gaps2[1], f"gaps={gaps2}")
check("补齐的间隔落在 [80, 400] ms 内",
      all(80 <= g <= 400 for g in gaps2), f"gaps={gaps2}")

print("\n== 稳定性：连续两轮不产出相同的时间戳序列 ==")
seqs = set()
for _ in range(10):
    evs, _ = chat_request_events("u1", "nick", "conv", "hello", "world")
    report(FakeSession(), "u1", "nick", evs)
    seqs.add(tuple(e["timestamp"] for e in captured["payload"]))
check("10 轮产出 10 个不同序列（无重复）", len(seqs) == 10, f"仅 {len(seqs)} 个")

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
