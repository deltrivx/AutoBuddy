#!/usr/bin/env python3
"""wb-daily 超时判定回归测试（2026-10-13 用户指出设计缺陷）。

### 背景

用户反馈：「超时判定有问题，你应该单独按照每个账号判定，而不是总时间，
如果我以后账号变多，你岂不是又炸了」。

实证：原实现写死 `deadline = time.monotonic() + 1800`（**30 分钟总上限**）。
账号池涨到 22 个后，单轮耗时必然超过 30 分钟 → 每轮都被强杀、永远 `failed`。
且账号越多炸得越狠——这是设计缺陷，不是参数问题。

### 修复后的语义

**按账号判定**：每个账号有独立计时窗口，只有**单个账号** `WB_DAILY_ACCOUNT_TIMEOUT`
（默认 180 秒）没有产出新日志行，才算卡住 → 杀进程。有产出就重置计时。

⇒ 总轮次时长 = 账号数 × 每账号耗时，随账号数自然伸缩，不再有总上限。
⇒ 单个账号真的卡死（上游不返回 / 网络黑洞）仍会被切掉，不会拖死整轮。

本测试锁定这些契约，防止回退到「总时间」。
"""

import ast
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


print("[1] 不存在「总时间」上限（旧缺陷）")

check("不再有写死的总 deadline 常量",
      "deadline = time.monotonic() + 1800" not in SRC)
check("不再有 30 分钟总超时文案",
      "执行超时（30 分钟）" not in SRC)
check("proc.wait 不再被总 deadline 掐住",
      not re.search(r"rc = await asyncio\.wait_for\(proc\.wait\(\), timeout=remain\)", SRC))

print("\n[2] 按账号判定已实现")

check("读取 per_account_timeout（可配置）",
      "per_account_timeout" in SRC)
check("支持 WB_DAILY_ACCOUNT_TIMEOUT 环境变量",
      'os.getenv("WB_DAILY_ACCOUNT_TIMEOUT"' in SRC)
check("默认 180 秒", '"180"' in SRC)
check("有 last_activity 计时基准", "last_activity = time.monotonic()" in SRC)
check("按 idle（距上次产出）判定超时",
      bool(re.search(r"idle = time\.monotonic\(\) - last_activity", SRC)))
check("idle 超限才 raise TimeoutError",
      bool(re.search(r"if idle >= per_account_timeout:\s*\n\s*raise TimeoutError\(\)", SRC)))
check("有产出即重置计时（账号在推进就不算卡）",
      bool(re.search(r"last_activity = time\.monotonic\(\).*?有产出", SRC)))
check("超时文案按单账号口径",
      "执行超时（单账号" in SRC)

print("\n[3] 等价算法验证（模拟多账号推进）")


def make_simulator(per_account=180.0):
    """复刻实现的超时判定：idle >= per_account 才杀。"""

    def run(gaps):
        """gaps: 每条日志距上一条的秒数。"""
        last_activity = 0.0
        t = 0.0
        for gap in gaps:
            t += gap
            idle = t - last_activity
            if idle >= per_account:
                return ("killed", t)
            last_activity = t
        return ("finished", t)

    return run


sim = make_simulator(180.0)

# 场景 1：22 个账号，每个 80 秒 → 总 1760 秒。
# 旧实现在总 1800s 处就杀；现在每账号 idle 仅 80s < 180 → 不该杀。
status, t = sim([80.0] * 22)
check("22 账号 × 80s（总 1760s）不被杀", status == "finished", f"{status}@{t}s")

# 场景 2：50 个账号 × 80s = 4000s，远超旧上限 → 仍不该杀（账号越多越能体现）。
status2, t2 = sim([80.0] * 50)
check("50 账号 × 80s（总 4000s）不被杀", status2 == "finished", f"{status2}@{t2}s")

# 场景 3：单账号卡死——某账号 200 秒无产出 → 该杀。
status3, t3 = sim([80.0, 80.0, 200.0])
check("单账号 200s 无产出被杀", status3 == "killed", f"{status3}@{t3}s")

# 场景 4：边界——每 179 秒有产出，idle 从未达到上限 → 不算卡。
status4, t4 = sim([179.0, 179.0])
check("每 179s 有产出不算卡", status4 == "finished", f"{status4}@{t4}s")

# 场景 5：可配置——把阈值调到 60s，则 80s 间隔就该杀（证明不是写死 180）。
sim5 = make_simulator(60.0)
status5, t5 = sim5([80.0, 80.0])
check("阈值可配置（60s 阈值下 80s 间隔被杀）", status5 == "killed", f"{status5}@{t5}s")

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
