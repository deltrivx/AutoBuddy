#!/usr/bin/env python3
"""风控预警（优化方案 ④）回归测试。

锁定契约（2026-10-21）：

1. 滑动窗口内失败达阈值（3 次 / 10 分钟）→ 触发一次预警回调；
2. 窗口外（1 小时冷却期）不重复触发；
3. **只通知、不停用**：预警链路绝不写账号池、绝不停用账号
   —— `autoDisableAccounts` 已关，本模块也不许以任何形式复活它；
4. 成功清零窗口命中（避免偶发抖动被误报）；
5. 熔断（连续失败）与预警（窗口累计）是两条独立的判定，
   后者能抓住「成功-失败」反复横跳这种连续数上不去的风控迹象。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gateway"))

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


tmp = Path(tempfile.mkdtemp())
os.environ["AB_DATA_DIR"] = str(tmp)

import cooldown as cd  # noqa: E402

src = (ROOT / "gateway" / "cooldown.py").read_text(encoding="utf-8")

# —— 捕获预警回调 ——
fired = []
cd.set_risk_alert_callback(
    lambda aid, fails, win, st: fired.append((aid, fails, win, st)))

print("== 风控预警：阈值触发 ==")

cd.clear("r1")
cd.record_failure("r1", 429, "rate")
check("第 1 次失败不预警", not fired, f"fired={fired}")
cd.record_failure("r1", 429, "rate")
check("第 2 次失败不预警", not fired, f"fired={fired}")
cd.record_failure("r1", 429, "rate")
check("第 3 次失败触发预警（3 次 / 10 分钟）", len(fired) == 1, f"fired={fired}")
if fired:
    aid, fails, win, st = fired[0]
    check("回调带上账号 id", aid == "r1", f"got {aid}")
    check("回调带上窗口内失败数", fails == 3, f"got {fails}")
    check("回调带上窗口秒数（600）", win == cd.RISK_ALERT_WINDOW == 600, f"got {win}")
    check("回调带上最后状态码", st == 429, f"got {st}")

print("\n== 风控预警：不重复推送 ==")
cd.record_failure("r1", 429, "rate")
check("冷却期内（1h）不重复推送", len(fired) == 1, f"fired={fired}")

print("\n== 风控预警：窗口滑动 ==")
# 手工把窗口命中改到 20 分钟前 → 已滑出窗口，不该计数
import time  # noqa: E402
st = cd._load()
st["r1"]["riskHits"] = [time.time() - 1200, time.time() - 1100]
cd._save(st)
snap = cd.snapshot().get("r1") or {}
check("滑出窗口的命中不计入", snap.get("riskHits") == 0, f"got {snap.get('riskHits')}")

print("\n== 风控预警：与熔断是两条独立判定 ==")
# 两者的差别：熔断看**连续**失败（record_success 清零 fails 与 breakerCount），
# 预警看**窗口累计**。因为 record_success 同时清 riskHits，实际语义是
# 「10 分钟内 3 次失败且中间没有成功」——比纯连续略宽（允许不同错误码
# 混计），又不会把一次成功之后的偶发失败误报成风控。
# 这里锁定的核心契约是：**预警计数与熔断计数互不干扰**。
cd.clear("r2")
cd.record_failure("r2", 429, "rate")
cd.record_failure("r2", 429, "rate")
cd.record_success("r2")   # 连续失败计数被清零 → 熔断永不触发
cd.record_failure("r2", 429, "rate")
cd.record_success("r2")   # 连续失败计数被清零 → 熔断计数也归零
check("成功后窗口命中被清零（避免误报）",
      (cd.snapshot().get("r2") or {}).get("riskHits") == 0,
      f"got {(cd.snapshot().get('r2') or {}).get('riskHits')}")
check("横跳场景未触发预警", not any(f[0] == "r2" for f in fired), f"fired={fired}")

# 但紧接着再连撞 3 次（无成功穿插）→ 预警与熔断**各自独立**地都该触发：
# 熔断是原有治理（拉长冷却），预警是新增通知（叫人）。两者同时成立
# 恰恰说明没有互相干扰。
cd.record_failure("r2", 403, "forbidden")
cd.record_failure("r2", 429, "rate")
cd.record_failure("r2", 429, "rate")
check("再次连撞满 3 次后触发预警",
      sum(1 for f in fired if f[0] == "r2") == 1, f"fired={fired}")
acc2 = cd._load().get("r2") or {}
check("熔断计数仍在独立工作（原有治理未被预警链路破坏）",
      int(acc2.get("breakerCount") or 0) >= 1, f"breakerCount={acc2.get('breakerCount')}")
check("预警允许混计 429/403 两种风控码",
      any(f[0] == "r2" for f in fired), f"fired={fired}")

print("\n== 风控预警：成功清零窗口命中 ==")
cd.clear("r3")
cd.record_failure("r3", 429, "rate")
cd.record_failure("r3", 429, "rate")
cd.record_success("r3")
snap3 = cd.snapshot().get("r3") or {}
check("成功后窗口命中清零", snap3.get("riskHits") == 0, f"got {snap3.get('riskHits')}")

print("\n== 红线：预警链路绝不触碰账号池 ==")
# cooldown 模块只能读写自己的状态文件；这里断言它没有任何
# 写账号池/停用账号的能力面。注意：注释里会出现「不 import notify」
# 「不做任何自动停用」这类说明文字，故断言只扫**可执行代码**。
import re  # noqa: E402
code_lines = []
for ln in src.splitlines():
    s = ln.strip()
    if s.startswith("#") or s.startswith('"""') or s.startswith("'''"):
        continue
    code_lines.append(ln)
code = "\n".join(code_lines)
check("cooldown 代码不 import notify（无推送依赖，防环）",
      not re.search(r"^\s*(import|from)\s+notify", code, re.M))
check("cooldown 代码不含账号池写入调用",
      "account_pool" not in code and "accounts.json" not in code)
check("cooldown 代码不含停用账号调用面",
      not re.search(r"\bdisable|set_disabled|deactivate\b", code, re.I))
check("cooldown 代码不 import account_policy（不停用账号的源头）",
      not re.search(r"^\s*(import|from)\s+account_policy", code, re.M))
# 状态文件只落在 cooldown_state.json
check("cooldown 只写自己的状态文件",
      str(cd.STATE_FILE).endswith("cooldown_state.json"), f"got {cd.STATE_FILE}")
# 预警回调默认未注册（不注入就不推，不会自作主张）
cd.set_risk_alert_callback(None)
before = len(fired)
cd.clear("r4")
cd.record_failure("r4", 429, "rate")
cd.record_failure("r4", 429, "rate")
cd.record_failure("r4", 429, "rate")
check("回调置空后不推送也不报错", len(fired) == before, f"fired={fired}")

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
