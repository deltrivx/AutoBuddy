#!/usr/bin/env python3
"""refresh_guard 刷新防护与熔断回归测试。

锁定四条契约（对应防复发四点）：
1. 单飞：同账号并发刷新只允许一次在飞，release 后可再次获得；
2. 失败隔离：refresh 报「issued before session started」/「is invalid」时
   标记「需人工重登」，其他错误不标记；成功后清除标记；
3. 熔断告警：窗口内失败数达阈值触发回调（带去重冷却）；
4. 节奏限制：最小间隔 + 每日上限。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


tmp = tempfile.mkdtemp()
import refresh_guard as rg  # noqa: E402

rg.DATA_DIR = Path(tmp)
rg.STATE_FILE = Path(tmp) / "refresh_state.json"

print("[1] 单飞：同账号并发只允许一次在飞")

check("首次 acquire 成功", rg.try_acquire("a1") is True)
check("同账号第二次 acquire 被拒", rg.try_acquire("a1") is False)
check("其他账号不受影响", rg.try_acquire("a2") is True)
rg.release("a1")
check("release 后可再次 acquire", rg.try_acquire("a1") is True)
rg.release("a1")
rg.release("a2")

print("\n[2] 失败隔离：session 重置 → 需人工重登")

r1 = rg.record_failure("b1", "Refresh token issued before session started")
check("issued before session started → 标记重登", r1["needsRelogin"] is True)
check("b1 在 needs_relogin_ids 中", "b1" in rg.needs_relogin_ids())

r2 = rg.record_failure("b2", "refresh token is invalid")
check("is invalid → 标记重登", r2["needsRelogin"] is True)

r3 = rg.record_failure("b3", "请求失败: connection timeout")
check("网络错误 → 不标记重登", r3["needsRelogin"] is False)
check("b3 不在 needs_relogin_ids 中", "b3" not in rg.needs_relogin_ids())

# 成功后清除标记
rg.record_success("b1")
check("成功后清除重登标记", rg.needs_relogin("b1") is False)
check("b1 移出 needs_relogin_ids", "b1" not in rg.needs_relogin_ids())

print("\n[3] 熔断告警：窗口内失败数达阈值触发回调")

calls = []


def _cb(count: int) -> None:
    calls.append(count)


rg.set_alert_callback(_cb)
# 阈值默认 5，先清空之前累积的失败事件（它们也在窗口内）
rg.STATE_FILE = Path(tmp) / "refresh_state2.json"

for i in range(4):
    rg.record_failure(f"c{i}", "refresh token is invalid")
check("4 个失败（<5）不触发告警", len(calls) == 0, f"calls={calls}")
rg.record_failure("c4", "refresh token is invalid")
check("第 5 个失败触发告警", len(calls) == 1, f"calls={calls}")
check("告警回调收到失败数 >= 5", calls[0] >= 5, f"got {calls[0]}")

# 冷却期内再失败不重复告警
rg.record_failure("c5", "refresh token is invalid")
check("告警冷却期内不重复触发", len(calls) == 1, f"calls={calls}")

print("\n[4] 节奏限制：最小间隔 + 每日上限")

rg.STATE_FILE = Path(tmp) / "refresh_state3.json"
check("首次刷新放行", rg.check_rate("d1") is None)
# 模拟一次成功刷新，记时间戳 + 计数
rg.record_success("d1")
deny = rg.check_rate("d1")
check("间隔内再次刷新被拒", deny is not None, f"got {deny}")

# 每日上限：直接写入 3 次计数后应拒绝
state = rg._load()
state["d2"] = {"daily": {rg._day_bucket(rg._now()): 3}}
rg._save(state)
deny2 = rg.check_rate("d2")
check("达每日上限被拒", deny2 is not None, f"got {deny2}")

print(f"\n===== {_ok} passed, {len(_fail)} failed =====")
if _fail:
    print("FAILED:", _fail)
    sys.exit(1)
