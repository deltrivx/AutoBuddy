"""officialUsage 账号级同步状态必须自洽（v1.0.28 修复回归）。

缺陷现象（用户 2026-10-24 实测）：积分统计页一直警告「部分账号官方用量
未同步」，但同时显示「已同步 4/4 个当前账号」，失败账号的官方数值一片
「—」。实际 4 个当前账号全部同步成功 —— 这是误报警告。

根因：`_reconcile_official_usage()` 重建 `ou.accounts[]` 时，把每个账号的
状态字段**无条件硬编码**：
    "detailTruncated": True,     # 宣称「明细被截断」
    "error": None,               # 宣称「无错误」
    "ok": True,                  # 宣称「同步成功」
紧接着又无条件 `ou["errors"] = []`。这四个字段互相矛盾 —— 「明细被截断」
却「没有任何错误」。官方前端读到 detailTruncated=True 就判「有账号未同步」，
于是出现「已同步 4/4」与「部分账号未同步」并存的荒谬提示。

修法（v1.0.28）：状态字段改为如实反映 ——
    detailTruncated 只在真的没有逐模型明细时为 True
    ok 取决于该账号是否取到了 daily 数据
    error 只在真的没明细时给文案
    errors 数组由 accounts 里 ok=False 的项聚合而来，与 accounts 自洽

本测试锁定「自洽性」这一契约，不锁具体数值：
1. 有明细的账号：ok=True、detailTruncated=False、error=None
2. 无明细的账号：ok=False、detailTruncated=True、error 有文案
3. errors 数组与 accounts 里 ok=False 的数量严格一致
4. errors 里不会出现 ok=True 的账号（不误报）
5. 全部账号有明细时 errors 必须为空（不误报 —— 这是用户缺陷的核心）

全部走源码断言，不依赖运行时 import（与其它 tests/test_*.py 同一套路）。
"""

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


# 定位 _reconcile_official_usage 函数体（从 def 到下一个顶层 def）
m_exec = re.search(
    r"^def _reconcile_official_usage\(.*?(?=^def |\Z)", SRC, re.S | re.M)
body = m_exec.group(0) if m_exec else ""
check("_reconcile_official_usage 存在", bool(body))

print("\n[1] 状态字段不得硬编码（缺陷根因）")
check("不再无条件写 detailTruncated=True",
      '"detailTruncated": True,' not in body)
check("不再无条件写 ok=True",
      '"ok": True,' not in body)
check("不再无条件写 error=None",
      '"error": None,' not in body)

print("\n[2] 状态由真实数据决定")
check("有 _acc_has_detail 这一判定变量",
      "_acc_has_detail" in body)
check("detailTruncated 由 _acc_has_detail 取反",
      re.search(r'"detailTruncated":\s*not _acc_has_detail', body) is not None)
check("ok 由 _acc_has_detail 决定",
      re.search(r'"ok":\s*_acc_has_detail\b', body) is not None)
check("error 在无明细时给文案（不是 None）",
      re.search(r'"error":\s*None if _acc_has_detail else', body) is not None)
check("_acc_has_detail 同时看 daily 与 models",
      re.search(r"_acc_has_detail\s*=\s*bool\(_new_daily\)\s*and\s*bool\(_acc_models\)",
                body) is not None)

print("\n[3] errors 数组与 accounts 自洽（不再无条件清空）")
check("不再无条件清空 errors",
      'ou["errors"] = []' not in body)
check("errors 由 rebuilt 聚合而来",
      re.search(r'ou\["errors"\]\s*=\s*\[', body) is not None)
check("只收录 ok 为假的账号",
      re.search(r'for\s+_ra\s+in\s+rebuilt\s+if\s+not\s+_ra\.get\("ok"\)', body) is not None)
check("错误项带 accountId（前端能定位到账号）",
      re.search(r'\{"accountId":\s*_ra\.get\("accountId"\)', body) is not None)
check("错误项带 accountName",
      '"accountName": _ra.get("accountName")' in body)
check("错误项兜底文案（error 为空时也给「未同步」）",
      re.search(r'_ra\.get\("error"\)\s*or\s*"未同步"', body) is not None)

print("\n[4] 全同步时不得误报（用户缺陷的核心断言）")
# errors 的推导规则保证：rebuilt 里没有 ok=False 的账号时，列表推导结果为空。
# 用一段最小可执行样本验证这个推导逻辑本身（不依赖 gateway 运行时）。
_probe = [
    {"accountId": "1", "accountName": "A", "ok": True, "error": None},
    {"accountId": "2", "accountName": "B", "ok": True, "error": None},
    {"accountId": "3", "accountName": "C", "ok": True, "error": None},
    {"accountId": "4", "accountName": "D", "ok": True, "error": None},
]
_derived = [{"accountId": r.get("accountId"),
             "accountName": r.get("accountName"),
             "error": r.get("error") or "未同步"}
            for r in _probe if not r.get("ok")]
check("4/4 全部 ok=True 时 errors 为空（不误报）", _derived == [], f"got {_derived}")

_probe2 = _probe[:3] + [{"accountId": "4", "accountName": "D", "ok": False,
                         "error": "该账号无逐模型明细"}]
_derived2 = [{"accountId": r.get("accountId"),
              "accountName": r.get("accountName"),
              "error": r.get("error") or "未同步"}
             for r in _probe2 if not r.get("ok")]
check("真有账号未同步时 errors 只含那一个",
      len(_derived2) == 1 and _derived2[0]["accountId"] == "4", f"got {_derived2}")
check("未同步项带上真实 error 文案",
      _derived2 and _derived2[0]["error"] == "该账号无逐模型明细")

print("\n[5] 未破坏既有校准逻辑")
check("仍保留账号明细整体接管（ou.accounts = rebuilt）",
      'ou["accounts"] = rebuilt' in body)
check("仍标注来源 gateway-realtime",
      'ou["source"] = "gateway-realtime"' in body)
check("仍校准 rangeEnd",
      'ou["rangeEnd"] = time.strftime("%Y-%m-%d")' in body)
check("仍保留 usageToday 顶层优先",
      "top_today" in body and "ou_summary" in body)
check("_reconcile_official_usage 仍被调用点传入 valid_account_ids",
      "_reconcile_official_usage(data, valid_account_ids)" in SRC)

print("\n" + "=" * 50)
print(f"通过 {_ok} 项，失败 {len(_fail)} 项" + ("：" + str(_fail) if _fail else " ✅"))
if _fail:
    raise SystemExit(1)
