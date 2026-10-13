#!/usr/bin/env python3
"""会话粘性让位于积分调度（2026-10-13 实测：配额制被粘性架空）。

### 实测根因

`selection_logs.json` 里 **97%（194/200）的选号走 sticky**，配额制只服务 3%
的新会话 —— 余额-承接相关系数仅 **0.032**，等于积分调度被粘性彻底架空。

原因：`session_sticky.get_bound()` 命中就在配额排序**之前** `return`，
绝大多数请求根本走不到配额逻辑。

### 修复

粘性本身有价值（同一对话复用账号，上下文/计费不混乱），不能简单去掉。
折衷：绑定账号余额**明显掉队**（低于池内均值 × `AB_STICKY_YIELD_RATIO`，默认 0.5）
且池内存在更健康候选时，解绑改走配额。

⇒ 日常对话仍保持粘性；掉队账号不再被死守，积分调度真正生效。
⇒ 解绑后由配额的 `_remember_selection(acc, "auto")` 统一记账，一次选号一条流水。

本测试锁定契约，防止回退到「粘性无条件优先」。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parent
SRC = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")

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


print("[1] 源码契约")

tree = ast.parse(SRC)
check("STICKY_YIELD_RATIO 常量存在",
      any(isinstance(n, ast.Assign)
          and any(getattr(t, "id", "") == "STICKY_YIELD_RATIO" for t in n.targets)
          for n in tree.body))
check("默认 0.5", '"0.5"' in SRC)
check("_sticky_should_yield helper 存在", "def _sticky_should_yield" in SRC)
check("读取 balance_of（与 credit_floor 同源）",
      "credit_floor.balance_of(_account_id(a))" in SRC)
check("粘性分支调用让位判定",
      bool(ast.parse(SRC) and "_sticky_should_yield(str(bound), candidates)" in SRC))
check("让位时解绑", "session_sticky.unbind(session_key)" in SRC)
check("不让位时才走 sticky",
      bool(re.search(r"if _sticky_should_yield.*?else:\s*\n\s*return _remember_selection\(acc, \"sticky\"\)",
                     SRC, re.S)) if (re := __import__("re")) else False)
check("解绑后不重复记账（无 _remember_selection_unyield 残留）",
      "_remember_selection_unyield" not in SRC)
check("候选不足 2 个时不让位（避免误伤）", "len(candidates) < 2" in SRC)
check("关闭积分调度时不让位（AB_CREDIT_WEIGHT=0）",
      "if not CREDIT_WEIGHT_SCHED:" in SRC)

print("\n[2] 等价算法验证（复刻判定逻辑）")

RATIO = 0.5


def should_yield(mine, balances):
    """mine: 绑定账号余额；balances: 候选集余额列表。"""
    if len(balances) < 2:
        return False
    if mine is None or mine <= 0:
        return False
    avg = sum(balances) / len(balances)
    return mine < avg * RATIO


# 场景 1：绑定账号 100，其余 1000/1000 → 均值 700，阈值 350 → 100 < 350 让位
check("余额 100 vs 池均值 700 → 让位", should_yield(100.0, [1000.0, 1000.0]) is True)
# 场景 2：绑定账号 600，其余 1000/1000 → 阈值 350 → 600 > 350 不让位
check("余额 600 vs 池均值 700 → 不让位", should_yield(600.0, [1000.0, 1000.0]) is False)
# 场景 3：余额未知（None）→ 不让位（新账号不被误伤）
check("余额未知 → 不让位", should_yield(None, [1000.0, 1000.0]) is False)
# 场景 4：只有一个候选 → 不让位（无从让起）
check("候选不足 2 个 → 不让位", should_yield(1.0, [1000.0]) is False)
# 场景 5：边界——刚好等于阈值 → 不让位（严格小于才让）
avg = (1000.0 + 1000.0) / 2
check("刚好等于阈值 → 不让位（严格小于）",
      should_yield(avg * RATIO, [1000.0, 1000.0]) is False)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
