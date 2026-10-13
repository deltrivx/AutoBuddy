#!/usr/bin/env python3
"""积分感知调度回归测试（2026-10-13：配额制 · 按余额比例分摊）。

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
check("排序使用配额制（已选次数/配额比例）",
      "_picked.get(aid, 0) / quota" in SRC)
check("余额读取走 credit_floor.balance_of（同源快照）",
      "credit_floor.balance_of(_account_id(a))" in SRC)
check("未知余额取池均值（不惩罚新账号）",
      "_bal.get(aid, _avg)" in SRC)
check("权重下限 0.05 防除零", "0.05" in SRC)
check("异常兜底退回纯 inflight 排序",
      SRC.count("ordered.sort(key=lambda a: int(inflight.get(") >= 2)
check("只统计正余额（None/0 不进权重表）",
      "_v > 0" in SRC)
check("权重差封顶 CAP 存在", "_WEIGHT_CAP" in SRC)

print("\n[2] 等价算法验证（复刻排序语义）")


CAP = 8.0


def make_pick(balances):
    """复刻配额制选号（2026-10-13 二次修正后的实现）。"""
    picked = {}

    def pick(candidates, inflight):
        known = {a: v for a, v in balances.items() if v > 0}
        if not known:
            return sorted(candidates, key=lambda a: inflight.get(a, 0))[0]
        avg = sum(known.values()) / len(known)

        def w(a):
            return max(known.get(a, avg) / avg, 0.05)

        def k(a):
            q = min(w(a), CAP)
            return (picked.get(a, 0) / q, inflight.get(a, 0))

        c = sorted(candidates, key=k)[0]
        picked[c] = picked.get(c, 0) + 1
        return c

    return pick


# 通用模拟器：带在飞衰减（更接近真实并发场景）
import random


def simulate(balances, cands, rounds=400, decay=0.35, seed=7):
    random.seed(seed)
    pick = make_pick(balances)
    inf = {c: 0 for c in cands}
    cnt = {c: 0 for c in cands}
    for _ in range(rounds):
        p = pick(cands, inf)
        cnt[p] += 1
        inf[p] += 1
        for k2 in inf:
            while inf[k2] > 0 and random.random() < decay:
                inf[k2] -= 1
    return cnt


cands = ["A", "B"]

# 场景 1：A 余额 3000、B 1000 → 配额 3:1 → 稳态约 75% : 25%
c1 = simulate({"A": 3000.0, "B": 1000.0}, cands)
check("A(3000) 承接显著多于 B(1000)", c1["A"] > c1["B"] * 2, f"A={c1['A']} B={c1['B']}")
check("低余额号仍被选中（非零）", c1["B"] > 20, f"B={c1['B']}")

# 场景 2：A 衰减到 500、B 仍 1000 → B 反超（用户核心诉求）
c2 = simulate({"A": 500.0, "B": 1000.0}, cands)
check("A(500) 衰减后 B(1000) 反超", c2["B"] > c2["A"], f"A={c2['A']} B={c2['B']}")

# 场景 3：同余额 → 约 1:1（轮询均匀）
c3 = simulate({"A": 1000.0, "B": 1000.0}, cands)
check("同余额约 1:1（轮询均匀）", abs(c3["A"] - c3["B"]) < 80, f"A={c3['A']} B={c3['B']}")

# 场景 4：未知余额（C 不在快照）→ 中性权重，仍可被选中
c4 = simulate({"A": 4000.0}, ["A", "B", "C"])
check("未知余额号不被饿死（B/C 均被选中）",
      c4["B"] > 20 and c4["C"] > 20, f"A={c4['A']} B={c4['B']} C={c4['C']}")

# 场景 5：无余额数据 → 退回纯 inflight
p5 = make_pick({})
check("无余额数据退回 inflight 最少", p5(cands, {"A": 3, "B": 1}) == "B")

# 场景 6：权重差封顶（CAP）——差 40 倍也不至于 100:0
c6 = simulate({"A": 20000.0, "B": 500.0}, cands)
check("极端差额下低余额号仍有份额（CAP 封顶后约 2%）", c6["B"] > 3,
      f"A={c6['A']} B={c6['B']}")

print(f"\n{'='*52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
