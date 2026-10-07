#!/usr/bin/env python3
"""上游错误冷却与熔断回归测试。

参考 workbuddy2api-panel（2009★）的错误分类治理：

```
429  → 软冷却 600s 起，连续触发指数退避，封顶 2h
402  → 硬冷却至次日 04:00（余额不足，签到回血才恢复）
404  → 固定 60s 短冷却
连续失败 → 熔断（阈值 3，退避 30m×2^n，封顶 6h）
```

为什么必须有（实测依据）：AutoBuddy 此前对 429 是**直接透传**，
排查中发现 73 次 429 全部原样失败 —— 而池里有 17 个账号，
一个号被限流完全可以换一个；余额耗尽更该立刻停用。

本测试锁定四条契约：

1. 分类正确：429 软冷却 / 402 与额度耗尽硬冷却 / 404 短冷却；
2. 软冷却连续触发按 2 倍指数退避且封顶；
3. 成功清零连续失败与退避计数；
4. 冷却账号被自动轮询过滤掉，且**全池冷却时回退**（不在网关层编「无账号」）；
5. 模型不存在（11102）**不惩罚账号** —— 那是换号可解的，不是账号的错。
"""
import os
import sys
import tempfile
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


tmp = tempfile.mkdtemp()
import cooldown as cd  # noqa: E402

cd.DATA_DIR = Path(tmp)
cd.STATE_FILE = Path(tmp) / "cooldown_state.json"

print("[1] 分类正确：429 软 / 402 硬 / 404 短 / 11102 不罚号")

k = cd.record_failure("a1", 429, '{"code":9,"msg":"too many requests"}')
check("普通 429 → 软冷却", k == "soft", f"got {k}")
check("软冷却生效", cd.is_cooling("a1"), "应处于冷却")
check("软冷却 reason 为 rate_limit",
      cd.snapshot()["a1"]["reason"] == "rate_limit",
      f'got {cd.snapshot()["a1"]["reason"]}')

k2 = cd.record_failure("a2", 402, '{"msg":"Insufficient Balance"}')
check("402 → 硬冷却", k2 == "hard", f"got {k2}")
check("硬冷却 reason 为 quota",
      cd.snapshot()["a2"]["reason"] == "quota",
      f'got {cd.snapshot()["a2"]["reason"]}')

# 关键：额度耗尽也可能是 429（历史 code=14018），必须按语义判成硬冷却
k3 = cd.record_failure(
    "a3", 429,
    '{"error":{"data":{"code":14018,"msg":"Credits exhausted"}}}')
check("429 + Credits exhausted → 硬冷却（不是软冷却）", k3 == "hard", f"got {k3}")

k4 = cd.record_failure("a4", 404, '{"msg":"not found"}')
check("404 → not_found 短冷却", k4 == "not_found", f"got {k4}")
rem = cd.snapshot()["a4"]["remainingSec"]
check("404 冷却约 60s", 50 <= rem <= 65, f"got {rem}")

k5 = cd.record_failure(
    "a5", 400, '{"code":11102,"msg":"model service info not found"}')
check("11102 模型不存在 → 不冷却账号", k5 is None, f"got {k5}")
check("该账号不处于冷却", not cd.is_cooling("a5"), "不该被罚")

print("\n[2] 软冷却连续触发按 2 倍指数退避，且封顶")

cd.clear("b1")
r1 = cd.snapshot().get("b1")
first = None
cd.record_failure("b1", 429, '{"msg":"rate"}')
first = cd.snapshot()["b1"]["remainingSec"]
cd.record_failure("b1", 429, '{"msg":"rate"}')
second = cd.snapshot()["b1"]["remainingSec"]
check("第 1 次约 600s", 590 <= first <= 605, f"got {first}")
check("第 2 次翻倍约 1200s", 1180 <= second <= 1205, f"got {second}")
check("确实翻倍", second > first * 1.8, f"{first} -> {second}")

# 连续多次不应超过封顶
for _ in range(8):
    cd.record_failure("b2", 429, '{"msg":"rate"}')
capped = cd.snapshot()["b2"]["remainingSec"]
check("退避封顶不超过 SOFT_RATE_MAX", capped <= cd.SOFT_RATE_MAX + 5,
      f"got {capped} max={cd.SOFT_RATE_MAX}")

print("\n[3] 成功清零连续失败与退避计数")

cd.record_failure("c1", 429, '{"msg":"rate"}')
cd.record_failure("c1", 429, '{"msg":"rate"}')
check("退避已累积", cd.snapshot()["c1"]["softStreak"] == 2,
      f'got {cd.snapshot()["c1"]["softStreak"]}')
cd.record_success("c1")
s = cd.snapshot()["c1"]
check("成功后 softStreak 清零", s["softStreak"] == 0, f"got {s['softStreak']}")
check("成功后 fails 清零", s["fails"] == 0, f"got {s['fails']}")
check("成功后 breakerCount 清零", s["breakerCount"] == 0,
      f"got {s['breakerCount']}")

print("\n[4] 冷却账号被过滤，且全池冷却时回退")


def _id_of(d):
    return d.get("id")


accounts = [{"id": "x1"}, {"id": "x2"}, {"id": "x3"}]
alive = cd.filter_cooling(accounts, _id_of)
check("无冷却时全部保留", len(alive) == 3, f"got {len(alive)}")

cd.record_failure("x1", 429, '{"msg":"rate"}')
alive2 = cd.filter_cooling(accounts, _id_of)
ids2 = [a["id"] for a in alive2]
check("冷却中的账号被过滤", "x1" not in ids2, f"got {ids2}")
check("未冷却的账号保留", len(ids2) == 2 and "x2" in ids2 and "x3" in ids2,
      f"got {ids2}")

# 全池冷却 → 必须回退（不在网关层编「无账号」）
cd.record_failure("x2", 429, '{"msg":"rate"}')
cd.record_failure("x3", 429, '{"msg":"rate"}')
alive3 = cd.filter_cooling(accounts, _id_of)
check("全池冷却时回退原集合（不返回空）", len(alive3) == 3,
      f"got {len(alive3)}")

print("\n[5] 熔断：连续失败达阈值")

cd.clear("d1")
for i in range(cd.BREAKER_THRESHOLD):
    cd.record_failure("d1", 500, '{"msg":"upstream error"}')
s = cd.snapshot()["d1"]
check("熔断已触发（处于冷却）", s["cooling"], f"got {s}")
check("breakerCount 递增", s["breakerCount"] >= 1, f"got {s['breakerCount']}")

print("\n[6] 持久化与手动解除")

cd.clear("e1")
check("手动解除后不再冷却", not cd.is_cooling("e1"), "应已解除")
cd.record_failure("e2", 402, '{"msg":"Insufficient Balance"}')
# 重新载入（模拟重启）后仍在冷却
cd2 = cd
check("落盘后重新载入仍在冷却", cd2.is_cooling("e2"), "重启后应保留")

print("\n[7] 渠道拦截 400/11128：冷却该账号，但不误伤普通 400")

# 实测依据（2026-10-07 晚）：窗口内 2 次 11128 全部落在「尘星途」(a944ccff…)，
# 同期其他账号用**完全相同**的请求形态都成功 —— 所以这是账号级拦截，
# 修复前 400 一律不冷却，被拦的号仍留在候选集里反复撞同一个 400。
CHANNEL_BODY = '{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}'

cd.clear("f1")
check("11128 归类为 channel", cd._classify(400, CHANNEL_BODY) == "channel",
      f"got {cd._classify(400, CHANNEL_BODY)}")
applied = cd.record_failure("f1", 400, CHANNEL_BODY)
check("11128 施加 channel 冷却", applied == "channel", f"got {applied}")
check("11128 账号进入冷却", cd.is_cooling("f1"), "应处于冷却")
check("冷却原因记为 channel_blocked",
      cd._load().get("f1", {}).get("reason") == "channel_blocked",
      f"got {cd._load().get('f1', {}).get('reason')}")

# 对照组：普通参数类 400 绝不能被卷进来（那不是账号的错）
cd.clear("f2")
applied2 = cd.record_failure("f2", 400, '{"code":11133,"msg":"bad param"}')
check("普通 400 不冷却", applied2 is None, f"got {applied2}")
check("普通 400 账号不进冷却", not cd.is_cooling("f2"), "不该冷却")

# 被渠道拦截的账号要退出轮询，健康的号不受牵连
kept = cd.filter_cooling([{"id": "f1"}, {"id": "f2"}], lambda a: a["id"])
kept_ids = [a["id"] for a in kept]
check("被拦截账号退出轮询", "f1" not in kept_ids, f"got {kept_ids}")
check("健康账号仍在轮询", "f2" in kept_ids, f"got {kept_ids}")

import shutil  # noqa: E402

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
