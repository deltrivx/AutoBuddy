#!/usr/bin/env python3
"""调度算法三项优化回归（2026-10-14，对齐 Smart Load Balancer）。

### 实测问题（对比 Azure Smart Load Balancer 后定位）

| 问题 | 实测数据 |
|---|---|
| ① `credit_picks` 纯累计、**永不衰减** | 重启才清零，相关系数在 +0.032~+0.083 反复摆动 |
| ② sticky 96% 旁路且**不记账** | auto 只服务 4% 流量；吃满粘性流量的账号比值反而偏低→正反馈 |
| ③ 冷却时长靠猜，不读 `Retry-After` | 429 软冷却 600s 起，上游给了准确时长却不用 |

### 实施

1. **时间衰减**：`credit_picks` 改为 `{n, t}` 指数衰减账本（半衰期
   `AB_CREDIT_PICK_HALFLIFE`，默认 1800s）+ 剪枝；
2. **sticky 记账 + 配额仲裁**：sticky 命中记入同一账本；绑定账号「有效已选/配额」
   劣于最优候选超过 `AB_STICKY_QUOTA_FACTOR`（默认 1.5）倍时解绑走配额；
3. **Retry-After**：`cooldown.parse_retry_after()` 解析秒数/HTTP 日期，
   与退避取**更晚**者并封顶 `SOFT_RATE_MAX`。

本测试锁定这些契约，防止回退。
"""

import ast
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
SRC_M = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
SRC_C = (ROOT / "gateway" / "cooldown.py").read_text(encoding="utf-8")

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


print("[1] ① 时间衰减账本")

check("CREDIT_PICK_HALFLIFE 常量存在", "CREDIT_PICK_HALFLIFE = " in SRC_M)
check("默认 1800s", '"1800"' in SRC_M)
check("_credit_pick_effective_locked 存在", "def _credit_pick_effective_locked" in SRC_M)
check("_record_credit_pick_locked 存在", "def _record_credit_pick_locked" in SRC_M)
check("带锁/无锁两个入口", "def _credit_pick_effective(" in SRC_M and "def _record_credit_pick(" in SRC_M)
check("指数衰减公式（0.5 ** 半衰期比）", "0.5 ** (elapsed /" in SRC_M)
check("账本结构含时间戳 t", 'picks[aid] = {"n": prev + 1.0, "t": now}' in SRC_M)
check("剪枝函数存在", "def _prune_credit_picks_locked" in SRC_M)
check("配额排序用衰减后的有效值",
      "_credit_pick_effective_locked(aid, _pick_now) / quota" in SRC_M)
check("配额记账走衰减账本",
      "_record_credit_pick_locked(str(_account_id(ordered[0])), _pick_now)" in SRC_M)

print("\n[2] ② sticky 记账 + 配额仲裁")

check("STICKY_QUOTA_ARBITRATION 常量", "STICKY_QUOTA_ARBITRATION = " in SRC_M)
check("STICKY_QUOTA_FACTOR 常量（默认 1.5）", 'STICKY_QUOTA_FACTOR = float(os.getenv("AB_STICKY_QUOTA_FACTOR", "1.5")' in SRC_M)
check("sticky 命中即记账", "_record_credit_pick(str(bound))" in SRC_M)
check("仲裁读有效已选", "_credit_pick_effective(aid, now) / min(w, _WEIGHT_CAP)" in SRC_M)
check("绑定账号已是最优则不让位", 'if best_aid == str(bound_id):' in SRC_M)
check("关闭开关时不仲裁", "if not STICKY_QUOTA_ARBITRATION:" in SRC_M)
check("余额掉队条件保留（旧契约不回退）", "float(mine) < avg * STICKY_YIELD_RATIO" in SRC_M)
check("STICKY_QUOTA_MIN_GAP 常量存在", "STICKY_QUOTA_MIN_GAP = " in SRC_M)
check("MIN_GAP 默认 1.0", '"AB_STICKY_QUOTA_MIN_GAP", "1.0"' in SRC_M)
check("绝对下限判定用 <=（严格大于才让位）",
      "(mine_ratio - best_ratio) <= STICKY_QUOTA_MIN_GAP" in SRC_M)
check("MIN_GAP 与倍数阈值同时生效（两个条件是 AND）",
      "if (mine_ratio - best_ratio) <= STICKY_QUOTA_MIN_GAP:\n            return False\n        return mine_ratio > best_ratio * STICKY_QUOTA_FACTOR" in SRC_M)

print("\n[3] ③ Retry-After 解析")

check("parse_retry_after 存在", "def parse_retry_after" in SRC_C)
check("record_failure 接受 retry_after 参数",
      "retry_after: Optional[float] = None" in SRC_C)
check("软冷却分支采纳 Retry-After", 'acc["retryAfterAt"] = until' in SRC_C)
check("取更晚者（保守）", "until = max(until, min(now + float(retry_after), now + SOFT_RATE_MAX))" in SRC_C)
check("封顶 SOFT_RATE_MAX 防异常值", "now + SOFT_RATE_MAX" in SRC_C)
check("_note_upstream_result 透传 headers", "headers: Optional[Any] = None" in SRC_M)
check("转发路径带上 res.headers", SRC_M.count("rid=rid, headers=res.headers") == 2,
      f"got {SRC_M.count('rid=rid, headers=res.headers')}")

print("\n[4] parse_retry_after 行为验证（真实模块）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
sys.path.insert(0, str(ROOT))
from gateway import cooldown as cd  # noqa: E402

check("秒数形式", cd.parse_retry_after("120") == 120.0, f"got {cd.parse_retry_after('120')}")
check("带空白的秒数", cd.parse_retry_after("  45 ") == 45.0)
check("空值 → None", cd.parse_retry_after(None) is None and cd.parse_retry_after("") is None)
check("非数字垃圾 → None", cd.parse_retry_after("abc") is None)
# HTTP 日期形式：取未来 1 小时
import datetime  # noqa: E402
future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
got = cd.parse_retry_after(http_date)
check("HTTP 日期形式（约 3600s）",
      got is not None and 3500 <= got <= 3660, f"got {got}")

print("\n[5] record_failure 采纳 Retry-After（真实 DB）")

cd.record_failure("ra1", 429, '{"msg":"rate"}', retry_after=900.0)
acc = cd.snapshot().get("ra1") or {}
check("Retry-After=900s → 冷却约 900s（而非默认 600s）",
      850 <= acc.get("remainingSec", 0) <= 910,
      f"got {acc.get('remainingSec')}s")
check("reason 仍为 rate_limit", acc.get("reason") == "rate_limit", f"got {acc.get('reason')}")

# 异常大值被 SOFT_RATE_MAX 封顶
cd.record_failure("ra2", 429, '{"msg":"rate"}', retry_after=999999.0)
acc2 = (cd.snapshot().get("ra2") or {})
check("异常大值封顶 SOFT_RATE_MAX",
      acc2.get("remainingSec", 0) <= cd.SOFT_RATE_MAX + 5,
      f"got {acc2.get('remainingSec')}s > {cd.SOFT_RATE_MAX}s")

# 不传 retry_after 时退回原退避
cd.record_failure("ra3", 429, '{"msg":"rate"}')
u3 = (cd.snapshot().get("ra3") or {})
check("不传 Retry-After 退回默认退避（约 600s）",
      550 <= u3.get("remainingSec", 0) <= 650, f"got {u3.get('remainingSec')}s")

print("\n[6] 衰减账本数值验证（复刻公式）")


def decayed(n, t, now, half_life):
    if n <= 0 or t <= 0:
        return 0.0
    e = now - t
    if e <= 0:
        return n
    return n * (0.5 ** (e / max(half_life, 1.0)))


check("刚记完 = 1", abs(decayed(1, 1000.0, 1000.0, 1800) - 1.0) < 1e-9)
check("一个半衰期后 = 0.5", abs(decayed(1, 1000.0, 2800.0, 1800) - 0.5) < 1e-9)
check("两个半衰期后 = 0.25", abs(decayed(1, 1000.0, 4600.0, 1800) - 0.25) < 1e-9)
check("三个半衰期后 ≈ 0.125", abs(decayed(1, 1000.0, 6400.0, 1800) - 0.125) < 1e-9)
check("时间倒流（t 在未来）→ 不放大", decayed(1, 2000.0, 1000.0, 1800) == 1.0)

print("\n[7] 配额仲裁边界（复刻修复后逻辑：AND 两个条件）")

FACTOR, MIN_GAP = 1.5, 1.0


def yields(mine, best):
    """复刻 _sticky_should_yield 的仲裁段（2026-10-14 修正）。"""
    if (mine - best) <= MIN_GAP:
        return False
    return mine > best * FACTOR


check("最优为 0 + 仅 1 次承接 → 不让位（MIN_GAP 防退化）",
      yields(1.0, 0.0) is False)
check("最优为 0 + 严重失衡（10 vs 0）→ 让位",
      yields(10.0, 0.0) is True)
check("完全持平（5 vs 5）→ 不让位", yields(5.0, 5.0) is False)
check("倍数失衡但差距不足（1.2 vs 1.0）→ 不让位",
      yields(1.2, 1.0) is False)
check("倍数失衡且差距够（3 vs 1，3 倍）→ 让位",
      yields(3.0, 1.0) is True)
check("差距够但倍数不足（2.1 vs 1.0，2.1 倍 > 1.5）→ 让位",
      yields(2.1, 1.0) is True)
check("绑定方已是最优（负差距）→ 不让位",
      yields(3.0, 5.0) is False)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
