"""officialUsage.status 行为契约（v1.0.33）。

背景（塔灵容器内实测，2026-10-24）：
  - /app/gateway/web_proxy.py:10943 无条件硬编码 ou["status"] = "partial"
  - 前端判定实证：function py(e){return e?.status==="complete"||e?.status==="partial"}
  - 「部分账号官方用量未同步」文案与 status=partial 对应
  - 其余字段全部正常（errors=[]、ok 全 true、detailTruncated 全 false）
  - requests 现为 600 条账本实时数据（_gateway_recent_requests）

本测试是**源码驱动的**：直接从 web_proxy.py 里抽出 status 赋值那一行，
代入 _g_reqs 的两种取值求值，断言真实产出的 status。这样测的是源码本身，
而不是手写的副本函数——避免"副本与源码脱节"的假绿。
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")

_ok = 0
_fail = []


def check(name, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name} {detail}")


# 抽出 _reconcile_official_usage 函数体里的 status 赋值行（源码驱动）
_lines = SRC.split("\n")
_status_lines = [
    (i + 1, ln.strip())
    for i, ln in enumerate(_lines)
    if re.search(r'ou\["status"\]\s*=', ln)
]
check("status 赋值点仅 1 处", len(_status_lines) == 1, f"got {len(_status_lines)}: {_status_lines}")
assert _status_lines, "找不到 status 赋值行"
_lineno, _stmt = _status_lines[0]
_expr = _stmt.split("=", 1)[1].strip()
check("status 赋值表达式可解析", bool(_expr), _expr)


def evaluate_status(g_reqs):
    """把源码里那一行当作表达式求值，g_reqs 对应源码中的 _g_reqs。"""
    env = {"_g_reqs": g_reqs}
    return eval(_expr, {"__builtins__": {}}, env)


print("\n[行为] status 随 _g_reqs 变化")
# 场景 A：账本接管（塔灵实测 requests=600 条）
reqs_600 = [{"requestId": f"r{i}", "model": "m", "credit": 1.0, "client": "gateway"} for i in range(600)]
st_a = evaluate_status(reqs_600)
check("场景A _g_reqs 有 600 条时 status 必须为 complete",
      st_a == "complete", f"got {st_a!r}（第 {_lineno} 行: {_stmt}）")
# 场景 B：账本为空
st_b = evaluate_status([])
check("场景B _g_reqs 为空时 status 必须为 partial",
      st_b == "partial", f"got {st_b!r}")
# 场景 C：单条也算接管
st_c = evaluate_status([{"requestId": "r1"}])
check("场景C _g_reqs 有 1 条时 status 为 complete", st_c == "complete", f"got {st_c!r}")

print("\n[安全] 必须仍在前端 py() 接受集合内")
# 官方前端: function py(e){return e?.status==="complete"||e?.status==="partial"}
_accept = ("complete", "partial")
check("场景A 产出的 status 在 complete|partial 内（不触发「官方用量暂不可用」）",
      st_a in _accept, f"got {st_a!r}")
check("场景B 产出的 status 在 complete|partial 内", st_b in _accept, f"got {st_b!r}")

print("\n[契约] 与 requests 写入条件同源")
# 10914 行: if _g_reqs: ou["requests"] = _g_reqs
check("源码中存在 if _g_reqs: 的 requests 写入（条件同源）",
      re.search(r'if\s+_g_reqs:\s*\n\s+ou\["requests"\]\s*=\s*_g_reqs', SRC) is not None)
check("status 表达式引用了 _g_reqs（不是恒定值）", "_g_reqs" in _expr, f"expr={_expr!r}")

print("\n" + "=" * 50)
print(f"通过 {_ok} 项，失败 {len(_fail)} 项" + ("：" + str(_fail) if _fail else " ✅"))
if _fail:
    raise SystemExit(1)
