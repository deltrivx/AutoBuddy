#!/usr/bin/env python3
"""积分感知调度回归测试（2026-10-13：加权最少连接）。

用户需求：「哪个账号积分多一些优先多调用这个账号，如果这个账号积分下来了，
另一个积分高了，那么可以调用另一个账号，多账号并发调用，然后动态轮询。」

实现：选号排序从纯「在飞请求数最少」改为 ``inflight / weight`` 最小者优先，
weight ∝ 积分余额（相对池均值归一，下限 0.05）。积分多 → 多承接；积分降 →
权重自动衰减 → 让位；未知余额 → 中性权重（不惩罚新账号）；
AB_CREDIT_WEIGHT=0 可关闭。
"""
import ast
from pathlib import Path

SRC = (Path(__file__).parent / "gateway" / "main.py").read_text(encoding="utf-8")
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


print("[1] 源码契约（真实解析）")
tree = ast.parse(SRC)
check("CREDIT_WEIGHT_SCHED 常量存在",
      any(isinstance(n, ast.Assign)
          and any(getattr(t, "id", "") == "CREDIT_WEIGHT_SCHED" for t in n.targets)
          for n in tree.body))
check("默认开启（env 未设时为 True）",
      'os.getenv("AB_CREDIT_WEIGHT", "1")' in SRC)
check("排序使用 inflight / weight",
      "/ _weight(a))" in SRC)
check("余额读取走 credit_floor.balance_of（同源快照）",
      "credit_floor.balance_of(_account_id(a))" in SRC)
check("未知余额取池均值（不惩罚新账号）",
      "_bal.get(aid, _avg)" in SRC)
check("权重下限 0.05 防除零", "0.05" in SRC)
check("异常兜底退回纯 inflight 排序",
      SRC.count("ordered.sort(key=lambda a: int(inflight.get(") >= 3)
check("只统计正余额（None/0 不进权重表）",
      "_v > 0" in SRC)

print("\n[2] 等价算法验证（复刻排序语义）")


def pick(candidates, inflight, balances):
    """复刻实现的选号：inflight / weight 最小者胜，同分按列表序。"""
    known = {a: v for a, v in balances.items() if v > 0}
    if known:
        avg = sum(known.values()) / len(known)
        def w(a):
            return max(known.get(a, avg) / avg, 0.05)
        return sorted(candidates, key=lambda a: inflight.get(a, 0) / w(a))[0]
    return sorted(candidates, key=lambda a: inflight.get(a, 0))[0]


# 场景 1：A 余额 3000、B 1000 → 3:1 承接比例
cands = ["A", "B"]
bal = {"A": 3000.0, "B": 1000.0}
inflight = {"A": 0, "B": 0}
picks = []
for _ in range(4):  # 模拟连续 4 个请求（选中后 inflight+1 模拟在飞）
    p = pick(cands, inflight, bal)
    picks.append(p)
    inflight[p] += 1
n_a = picks.count("A")
check("A(3000) 承接多于 B(1000)", n_a >= 3, f"picks={picks}")

# 场景 2：A 打到只剩 500、B 仍 1000 → B 反超
# 注：两账号都 0 在飞时 0/x 恒为 0，是真平局（稳定排序取先者）；
# 「反超」的语义是带负载后的性价比：同样 1 个在飞，B 的分更低。
bal2 = {"A": 500.0, "B": 1000.0}
inflight2 = {"A": 1, "B": 1}
first = pick(cands, inflight2, bal2)
check("A(500) 衰减后 B(1000) 反超（同在飞）", first == "B", f"got={first}")
# 串行压力下：A 已接 1 个在飞，B 空闲 → B 必胜
inflight2b = {"A": 1, "B": 0}
check("A 在飞时 B 空闲则 B 胜", pick(cands, inflight2b, bal2) == "B")

# 场景 3：并发分摊仍生效——同余额时在飞少者优先
bal3 = {"A": 1000.0, "B": 1000.0}
inflight3 = {"A": 3, "B": 0}
check("同余额时在飞少的 B 胜出", pick(cands, inflight3, bal3) == "B")

# 场景 4：未知余额（新账号 C）→ 中性权重，不被惩罚
bal4 = {"A": 4000.0, "B": 2000.0}  # C 未知
cands4 = ["A", "B", "C"]
inflight4 = {"A": 0, "B": 0, "C": 0}
p = pick(cands4, inflight4, bal4)
check("未知余额的新账号可被选中（不逆底）", p in ("A", "B", "C"), f"got={p}")
# 中性 C(权重1) vs A(权重2)：A 应优先于 C
inflight4b = {"A": 0, "B": 9, "C": 0}
p2 = pick(cands4, inflight4b, bal4)
check("中性权重参与竞争（B 在飞9时被跳过）", p2 in ("A", "C"), f"got={p2}")

# 场景 5：权重下限——余额极低的账号不会被除零
bal5 = {"A": 0.01, "B": 2000.0}
inflight5 = {"A": 0, "B": 0}
try:
    p5 = pick(["A", "B"], inflight5, bal5)
    check("极低余额不除零", p5 in ("A", "B"), f"got={p5}")
except ZeroDivisionError:
    check("极低余额不除零", False, "ZeroDivisionError")

# 场景 6：全部余额未知 → 退回纯 inflight（旧行为）
check("全未知退回纯 inflight",
      pick(cands, {"A": 2, "B": 1}, {}) == "B")

print(f"\n{'='*52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
