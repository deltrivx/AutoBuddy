"""自动留底的清理范围：只删时间戳形态，人工命名的一个不碰。

为什么需要这个测试：``_prune_accounts_backups`` 早期用
``glob("accounts.json.bak-*")``，那个通配符把**所有** ``accounts.json.bak-``
开头的文件都算进来。隔离实测（2026-10-21）确认它命中 11 个文件，其中 9 个是
人工命名的留证：

  - ``accounts.json.bak-rmmrh-20261020-200536``   唯一的 32 条干净源
  - ``accounts.json.bak-before-recovery-*``       污染态留证
  - ``accounts.json.bak-manual-*``                手工固化的备份
  - ``bak-deljunk-*`` / ``bak-prerestore-*`` / ``bak-before-*`` / ``bak-dead-*``

当时总数未超 ``_ACCOUNTS_BAK_KEEP``（20）所以侥幸没删，但一旦自动留底堆过
上限，这些**不可再生的人工留证**就会按 mtime 被当旧垃圾删掉。这个测试就是
那条护栏：用真目录、真文件、真跑 ``_prune_accounts_backups``，断言人工命名
的一个不少，时间戳形态的才受上限约束。

只依赖标准库 + 从 main.py 抽出的那两个纯函数，与其它 ``_test_*.py`` 一样
可以直接跑。
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
_ok = 0
_fail = []


def check(label, condition, detail=""):
    global _ok
    if condition:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _fail.append(label)
        print(f"  FAIL {label} {detail}")


# 与 _test_gateway_auth_gate.py 同一套手法：从 main.py 抽出留底相关的纯函数。
# main.py 依赖 fastapi 等未装包，整体 import 会失败，故只 exec 需要的片段。
#
# ⚠️ ns 必须提供 main.py 该片段用到的每一个顶层名字。v1.0.20 修 glob 误删时
# _prune_accounts_backups 改用 re.compile，而这里漏了 "re" —— 函数内部的
# NameError 被它自己的 except Exception: pass 吞掉，表现为「prune 一个都不删」
# 而没有任何报错。补上 re，并加一条断言防再漏。
src = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
ns: Dict[str, Any] = {
    "os": os, "json": json, "time": time, "shutil": shutil, "Path": Path,
    "Optional": Optional, "List": List, "Dict": Dict, "Any": Any, "print": print,
    "re": re,
}
seg = src[src.index("_ACCOUNTS_BAK_KEEP = 20"):src.index("def _load_pool_config(")]
seg = 'DATA_DIR = Path(os.environ.get("AB_TEST_DATA_DIR", "/tmp"))\n' + seg
exec(compile(seg, "prune_test", "exec"), ns)

_prune = ns["_prune_accounts_backups"]
_backup = ns["_backup_accounts"]
check("exec 切片命名空间完整（漏一个名字会被 except 吞成静默失败）",
      "_ACCOUNTS_BAK_KEEP" in ns and callable(_prune) and callable(_backup),
      f"ns keys={sorted(k for k in ns if not k.startswith('__'))[:20]}")

# 隔离临时目录，绝不指向真实数据目录。
TMP = Path(tempfile.mkdtemp(prefix="abprune-"))
os.environ["AB_TEST_DATA_DIR"] = str(TMP)


def make(path: Path, content: str = "[]", age_sec: float = 0):
    path.write_text(content, encoding="utf-8")
    if age_sec:
        t = time.time() - age_sec
        os.utime(path, (t, t))


print("== 1. 静态断言：清理范围被限定成时间戳形态 ==")
prune_src = src[src.index("def _prune_accounts_backups"):src.index("def _save_accounts")]
# 只查代码行（去掉 docstring），注释里提 glob 字样是说明历史，不算用 glob。
code_lines = []
in_doc = False
for ln in prune_src.split("\n"):
    s = ln.strip()
    if s.startswith('"""'):
        in_doc = not in_doc
        continue
    if in_doc or s.startswith("#"):
        continue
    code_lines.append(ln)
prune_code = "\n".join(code_lines)
check("不再用通配 glob（会把人工留证算进来）",
      "glob(" not in prune_code,
      "glob 会匹配所有 accounts.json.bak- 开头的文件，含人工命名的留证")
check("用正则按形态过滤", "re.compile" in prune_code or "re.match" in prune_code)
check("正则锚定 8 位日期 + 6 位时间",
      re.search(r"\\d\{8\}-\\d\{6\}", prune_code) is not None,
      r"应为 \d{8}-\d{6}")
check("同秒后缀 -1/-2 仍被认可（不能把自动留底自己漏掉）",
      re.search(r"\(-\\d\+\)\?", prune_code) is not None,
      r"应为 (-\d+)?")
check("正则用 ^...$ 全串锚定（防止前缀绕过）",
      'r"^"' in prune_code or "r'^'" in prune_code,
      "少了 ^ 会匹配到子串；$ 少了会让带尾巴的名字混进来")
check("★ 匹配用 fullmatch/match 全串语义（不是 search 子串）",
      "stamp_rx.match(" in prune_code or ".fullmatch(" in prune_code,
      "search 会让 accounts.json.bak-20261021-060000.evil 也混进清理范围")


print("\n== 2. 人工命名的留证，一个都不许被删 ==")
# 复刻真实存在过的 9 种人工命名 + 2 种自动留底形态。
MANUAL = [
    "accounts.json.bak-rmmrh-20261020-200536",        # 唯一的 32 条干净源
    "accounts.json.bak-before-recovery-20261021-141007",
    "accounts.json.bak-manual-20261021-1425",
    "accounts.json.bak-deljunk-20261020-222446",
    "accounts.json.bak-prerestore-20261020-224143",
    "accounts.json.bak-before-restore27-20261021-021135",
    "accounts.json.bak-before-add5-20261021-024239",
    "accounts.json.bak-dead-20261020-231513",
    "accounts.json.bak-restore-mrh-20261020-201912",
]
# 自动留底形态：故意造 25 份，超过 _ACCOUNTS_BAK_KEEP（20）
# 真实 stamp 来自 time.strftime("%Y%m%d-%H%M%S")，HHMMSS 恒为 6 位（小时补零），
# 这里用 060000..060024 复刻同一形态，保证 6 位不丢前导零。
AUTO = [f"accounts.json.bak-20261021-{60000 + i:06d}" for i in range(25)]

for name in MANUAL:
    make(TMP / name, "manual-keep")
for i, name in enumerate(AUTO):
    # 让 mtime 递增，最旧的排前面
    make(TMP / name, f"auto-{i}", age_sec=(25 - i) * 60)

primary = TMP / "accounts.json"
make(primary, "[]")

before_manual = {n: (TMP / n).read_text() for n in MANUAL}
before_auto = set(AUTO)

_prune(primary)

after = {p.name for p in TMP.iterdir() if p.is_file()}
missing_manual = [n for n in MANUAL if n not in after]
check("★ 9 个人工命名留证全部幸存", not missing_manual,
      f"被误删: {missing_manual}")
check("★ 人工留证内容未被改动",
      all((TMP / n).read_text() == before_manual[n] for n in MANUAL if (TMP / n).exists()))
check("★ 最老的那份 32 条干净源（bak-rmmrh）还在",
      "accounts.json.bak-rmmrh-20261020-200536" in after)
check("★ 污染态留证（bak-before-recovery）还在",
      "accounts.json.bak-before-recovery-20261021-141007" in after)

print("\n== 3. 自动留底仍受上限约束（不能因为怕误删就完全不清理） ==")
survived_auto = [n for n in AUTO if n in after]
check("自动留底被压到 _ACCOUNTS_BAK_KEEP 份",
      len(survived_auto) == ns["_ACCOUNTS_BAK_KEEP"],
      f"幸存 {len(survived_auto)} 份，上限 {ns['_ACCOUNTS_BAK_KEEP']}")
check("删掉的是最旧的那些（mtime 小的先删）",
      all(n not in after for n in AUTO[:5]),
      f"AUTO[:5]={AUTO[:5]}")
check("保留的是最新的那些",
      all(n in after for n in AUTO[-ns["_ACCOUNTS_BAK_KEEP"]:]))
check("自动留底确实被删了一些（证明清理仍在工作）",
      len(before_auto - after) > 0)


print("\n== 4. 同秒后缀的自动留底也被认可 ==")
for p in TMP.iterdir():
    p.unlink()
make(primary, "[]")
# 同一秒内多次留底会带 -1/-2 后缀
for i in range(3):
    t = time.time()
    name = f"accounts.json.bak-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(t))}"
    if i:
        name += f"-{i}"
    make(TMP / name, f"same-sec-{i}", age_sec=0)
# 再造 20 份把总数推过上限
for i in range(20):
    make(TMP / f"accounts.json.bak-20261020-{i:06d}", f"filler-{i}", age_sec=(20 - i) * 60)

before_same = {p.name for p in TMP.iterdir() if "-" in p.name and p.name.endswith(("-1", "-2"))}
_prune(primary)
after2 = {p.name for p in TMP.iterdir() if p.is_file()}
# 同秒后缀那几份 mtime 最新，应该在保留集里
check("同秒带后缀的自动留底不被漏删（它们是刚生成的）",
      all(n in after2 for n in before_same),
      f"丢失: {[n for n in before_same if n not in after2]}")


print("\n== 5. _backup_accounts 生成的名字能被自己识别（不自伤） ==")
for p in TMP.iterdir():
    p.unlink()
make(primary, "v0")
made = _backup(primary)
check("_backup_accounts 返回了路径", made is not None)
check("生成名是时间戳形态", made is not None and
      re.fullmatch(r"accounts\.json\.bak-\d{8}-\d{6}(-\d+)?", made.name) is not None,
      f"got {made.name if made else None}")
# 再触发一次 prune，刚生成的这份必须还在
_prune(primary)
check("刚生成的留底不会被自己清理掉", made is not None and made.exists())


print("\n== 6. 别的文件的 bak 一律不碰 ==")
for p in TMP.iterdir():
    p.unlink()
make(primary, "[]")
OTHERS = [
    "refresh_state.json.bak-relogintest",
    "model_policy.json.bak-ghostclean",
    "account_pool_config.json.bak-fixwl",
    "account_policy.json.bak-cleanauto",
]
for n in OTHERS:
    make(TMP / n, "{}")
for i in range(25):
    make(TMP / f"accounts.json.bak-20261021-{i:06d}", "[]", age_sec=(25 - i) * 60)
_prune(primary)
after3 = {p.name for p in TMP.iterdir() if p.is_file()}
check("其他文件的 bak 全部幸存（正则带文件名锚定）",
      all(n in after3 for n in OTHERS),
      f"丢失: {[n for n in OTHERS if n not in after3]}")


print("\n== 7. 边界：空目录 / 无自动留底不抛异常 ==")
for p in TMP.iterdir():
    p.unlink()
make(primary, "[]")
try:
    _prune(primary)
    check("空目录（只有本体）不抛异常", True)
except Exception as e:
    check("空目录（只有本体）不抛异常", False, repr(e))

try:
    _prune(TMP / "nonexistent.json")
    check("primary 不存在也不抛异常", True)
except Exception as e:
    check("primary 不存在也不抛异常", False, repr(e))


shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 56)
print(f"通过 {_ok} 项，失败 {len(_fail)} 项" + ("：" + str(_fail) if _fail else " ✅"))
raise SystemExit(1 if _fail else 0)
