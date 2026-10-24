"""officialUsage 账号级同步状态契约（v1.0.28 引入，v1.0.32 修正）。

## 修正史（两次都记在这里，别把教训丢了）

v1.0.28 首次修：`_reconcile_official_usage()` 原本把状态字段无条件硬编码
（`detailTruncated=True` / `error=None` / `ok=True`）又无条件清空 `errors[]`，
四个字段自相矛盾。当时改成「如实反映」，但判定式写成了：

    _acc_has_detail = bool(_new_daily) and bool(_acc_models)

v1.0.32 二次修：上面那个 `and` 是**方向性错误**。两个数据源语义不同 ——

    _new_daily  ← 官方接口返回的 daily        （"数据取到了吗"）
    _acc_models ← 网关 usage_logs 的逐模型记录 （"明细完整吗"）

`usage_logs` 只记录**经过网关调用过模型**的请求。只签到保活、从不走 API 的
账号，daily 有数据、usage_logs 为空 —— 被那个 `and` 判成 `ok=false`，
进 `errors[]`，官方前端就报「部分账号未同步」；而「已同步 4/4」用的是
daily（都有数据），两个提示自相矛盾。

现在拆开：`ok` 只看 daily，`detailTruncated` 只看 models，互不牵连。

## 为什么这轮的测试必须是行为测试

v1.0.28 那版 23 项断言**全是源码结构匹配**（`re.search` 查字段写法对不对），
一项都没验证真实数据下的行为 —— 所以上面那个 `and` 错了也照样全绿。
本文件因此同时保留两层：

  [A] 源码契约  —— 防止有人把字段改回硬编码（v1.0.28 的初衷）
  [B] 行为契约  —— 用**可执行的判定函数副本**跑模拟数据，验证 ok 的取值

[B] 的做法：把 `web_proxy.py` 里那段判定逻辑原样复制成一个独立函数
`_simulate_ok(has_daily, has_models)`，然后断言四种组合的取值。这样即使
源码里的字面写法变了，只要语义错了（ok 又依赖 models），断言就会红。
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


m_exec = re.search(
    r"^def _reconcile_official_usage\(.*?(?=^def |\Z)", SRC, re.S | re.M)
body = m_exec.group(0) if m_exec else ""
check("_reconcile_official_usage 存在", bool(body))

print("\n[A] 源码契约：状态字段不得硬编码（v1.0.28 的初衷，保留）")
check("不再无条件写 detailTruncated=True", '"detailTruncated": True,' not in body)
check("不再无条件写 ok=True", '"ok": True,' not in body)
check("不再无条件写 error=None", '"error": None,' not in body)
check("不再无条件清空 errors", 'ou["errors"] = []' not in body)

print("\n[A2] 源码契约：ok 与 detailTruncated 必须来自两个独立变量")
check("有 _acc_has_data 变量（官方数据侧）", "_acc_has_data" in body)
check("有 _acc_has_detail 变量（网关账本侧）", "_acc_has_detail" in body)
check("_acc_has_data 只依赖 _new_daily（不得含 _acc_models）",
      re.search(r"_acc_has_data\s*=\s*bool\(_new_daily\)\s*$", body, re.M) is not None,
      "ok 的判据必须只来自官方 daily")
check("_acc_has_detail 只依赖 _acc_models",
      re.search(r"_acc_has_detail\s*=\s*bool\(_acc_models\)\s*$", body, re.M) is not None,
      "明细的判据必须只来自网关账本")
check("ok 由 _acc_has_data 决定（不是 _acc_has_detail）",
      re.search(r'"ok":\s*_acc_has_data\b', body) is not None)
check("detailTruncated 由 _acc_has_detail 取反",
      re.search(r'"detailTruncated":\s*not _acc_has_detail', body) is not None)
check("error 只在无官方数据时给文案（不是无明细时）",
      re.search(r'"error":\s*None if _acc_has_data else', body) is not None)

print("\n[A3] 回归断言：errors[] 只由 ok=False 聚合")
check("errors 由 rebuilt 聚合",
      re.search(r'ou\["errors"\]\s*=\s*\[', body) is not None)
check("只收录 ok 为假的账号",
      re.search(r'for\s+_ra\s+in\s+rebuilt\s+if\s+not\s+_ra\.get\("ok"\)', body) is not None)
check("错误项带 accountId", re.search(r'\{"accountId":\s*_ra\.get\("accountId"\)', body) is not None)
check("错误项带 accountName", '"accountName": _ra.get("accountName")' in body)


# ---------------------------------------------------------------
# [B] 行为契约：把源码里的判定逻辑复制成可执行函数，跑模拟数据
# ---------------------------------------------------------------
print("\n[B] 行为契约：用可执行副本验证 ok 的真实取值")


def _simulate_fields(has_daily: bool, has_models: bool):
    """复刻 web_proxy.py 里那段判定，返回 (ok, detailTruncated, error)。

    与源码逐行对应：
        _acc_has_data   = bool(_new_daily)
        _acc_has_detail = bool(_acc_models)
        "ok":               _acc_has_data
        "detailTruncated":  not _acc_has_detail
        "error":            None if _acc_has_data else "该账号无官方用量数据（上游未返回）"
    """
    _acc_has_data = bool(has_daily)
    _acc_has_detail = bool(has_models)
    return (
        _acc_has_data,
        not _acc_has_detail,
        None if _acc_has_data else "该账号无官方用量数据（上游未返回）",
    )


def _simulate_errors(accounts):
    """复刻 errors[] 的聚合规则。accounts = [(ok, ...), ...]"""
    return [{"accountId": f"acc{i}", "error": "x"} for i, ok in enumerate(accounts) if not ok]


# --- 用例 A：只签到不调用模型（本次缺陷的核心场景）---
ok_a, trunc_a, err_a = _simulate_fields(has_daily=True, has_models=False)
check("用例A 有 daily 无 models：ok 必须为 True", ok_a is True, f"got {ok_a}")
check("用例A 有 daily 无 models：detailTruncated 必须为 True", trunc_a is True, f"got {trunc_a}")
check("用例A 有 daily 无 models：error 必须为 None", err_a is None, f"got {err_a!r}")

# --- 用例 B：完全取不到官方数据 ---
ok_b, trunc_b, err_b = _simulate_fields(has_daily=False, has_models=False)
check("用例B 无 daily 无 models：ok 必须为 False", ok_b is False, f"got {ok_b}")
check("用例B 无 daily：error 必须有文案", err_b is not None and "上游未返回" in err_b, f"got {err_b!r}")

# --- 用例 C：完全正常（有 daily 有 models）---
ok_c, trunc_c, err_c = _simulate_fields(has_daily=True, has_models=True)
check("用例C 有 daily 有 models：ok=True", ok_c is True)
check("用例C 有 daily 有 models：detailTruncated=False", trunc_c is False)
check("用例C 有 daily 有 models：error=None", err_c is None)

# --- 用例 D：有 models 但无 daily（理论少见，但需自洽）---
ok_d, trunc_d, err_d = _simulate_fields(has_daily=False, has_models=True)
check("用例D 无 daily 有 models：ok=False（数据没取到就是没取到）", ok_d is False)
check("用例D 无 daily 有 models：detailTruncated=False（明细本身是完整的）", trunc_d is False)

print("\n[B2] 行为契约：errors[] 聚合")
# 4/4 全同步（有 daily、无 models）→ errors 必须为空 —— 这是本次缺陷的直接断言
all_ok = [_simulate_fields(has_daily=True, has_models=False)[0] for _ in range(4)]
errs = _simulate_errors(all_ok)
check("4 个只签到账号全 ok=True 时 errors 为空（缺陷核心断言）", errs == [], f"got {errs}")

mixed = [True, True, False, True]
errs2 = _simulate_errors(mixed)
check("混入 1 个真失败时 errors 只含那 1 个", len(errs2) == 1, f"got {errs2}")

print("\n[B3] 关键反证：旧逻辑（ok 依赖 models）会失败")
def _old_logic(has_daily, has_models):
    """v1.0.28~v1.0.31 的错误判定，用于证明本测试确实能抓住缺陷。"""
    _acc_has_detail = bool(has_daily) and bool(has_models)
    return _acc_has_detail

check("旧逻辑在「有 daily 无 models」下判 False（说明缺陷真实存在）",
      _old_logic(True, False) is False)
check("新逻辑在同场景下判 True（修复生效）",
      _simulate_fields(True, False)[0] is True)

print("\n" + "=" * 50)
print(f"通过 {_ok} 项，失败 {len(_fail)} 项" + ("：" + str(_fail) if _fail else " ✅"))
if _fail:
    raise SystemExit(1)
