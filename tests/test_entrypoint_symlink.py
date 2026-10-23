#!/usr/bin/env python3
"""entrypoint 双向链接防环回归（2026-10-14 token 统计页 500）。

### 实测根因

`/api/token-stats` 报 500，真实堆栈：

```
File "/app/gateway/web_proxy.py", line 6413, in token_stats_api
    stats = get_aggregated_token_stats()
File "/app/gateway/token_store.py", line 91, in init_db
    conn.executescript(...)
sqlite3.OperationalError: unable to open database file
```

现场勘查发现**符号链接成环**：

```
/data/.autobuddy/token_stats.db-wal -> /data/.wb-switch/token_stats.db-wal
/data/.wb-switch/token_stats.db-wal -> /data/.autobuddy/token_stats.db-wal   ← A→B→A
```

`readlink -f` 解析结果为 UNRESOLVABLE，SQLite 打开时拿到 ELOOP。

### 成因

`entrypoint.sh` 的两轮"双向映射"用 `[ ! -e ]` 判断目标是否已占用。
`-e` **跟随符号链接**，对悬空链接返回 false —— 于是反向那一轮把悬空链接
当成"不存在"，再建一条指回本目录的链接，与既有的正向链接互指成环。

时间线（现场 mtime）：05:05 正向轮建链 → SQLite 干净关闭删掉 wal 实体 →
`.wb-switch` 侧链接悬空 → 12:18 反向轮把它当不存在，又在 `.autobuddy`
建一条指向 `.wb-switch` 的链接 → 死循环。

### 修复（entrypoint.sh）

1. 只传播**真实实体**：源用 `[ -e ]`（悬空链接直接跳过）；
2. 目标已占用（实体**或**链接都算）→ 跳过，不重复建链；
3. SQLite 边车文件（`-wal` / `-shm` / `-journal`）**永不跨目录链接** ——
   它们是事务副产品，必须与主库同目录，链到别处必然打不开。

### 影响面

读路径（/api/token-stats）与写路径（token usage flush）**同时**中断。
修复前日志有 1839 条 `Error flushing token usage: unable to open database file`
（最后一条 05:02:45Z，即故障期间持续丢数据）；修复后零新增，
`usage_logs` 实时恢复写入。

本测试锁定上述三条契约，并真实执行修复后的逻辑验证不成环。
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")

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


def extract_link_segment():
    """取出 entrypoint 的链接段（函数定义 + 两轮循环），供沙箱执行。"""
    seg = ENTRYPOINT[ENTRYPOINT.index("link_if_absent() {"):]
    end = seg.index("\n\n\n")
    return seg[:end]


def run_link_logic(seg, a, b):
    """把目录前缀替换成沙箱路径后真实执行。"""
    script = seg.replace("/data/.autobuddy", str(a)).replace("/data/.wb-switch", str(b))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def find_bad_links(*dirs):
    """返回悬空或自环的链接名列表。"""
    bad = []
    for d in dirs:
        for f in d.iterdir():
            if not f.is_symlink():
                continue
            resolved = Path(os.path.realpath(f))
            if not resolved.exists():
                bad.append(f"{f.name}: dangling")
            elif str(f) == str(resolved):
                bad.append(f"{f.name}: self-loop")
    return bad


print("[1] 源码契约")

check("存在 link_if_absent 函数", "link_if_absent()" in ENTRYPOINT)
check("存在 is_sqlite_sidecar 函数", "is_sqlite_sidecar()" in ENTRYPOINT)
check("源只接受真实实体（[ -e ]）", '[ -e "$src" ] || return 0' in ENTRYPOINT)
check("目标占用判断含 -L（链接也算占用）",
      'if [ -e "$dst" ] || [ -L "$dst" ]; then return 0; fi' in ENTRYPOINT)
check("不再使用裸 [ ! -e ] 判占用（原 bug）",
      'if [ ! -e "/data/.wb-switch/$fname" ]' not in ENTRYPOINT)
check("SQLite 边车三后缀齐全",
      all(s in ENTRYPOINT for s in ("*-wal", "*-shm", "*-journal")))
check("两轮循环都跳过边车文件", ENTRYPOINT.count("is_sqlite_sidecar") >= 3)
check("循环体加了存在性守卫", '[ -e "$f" ] || [ -L "$f" ] || continue' in ENTRYPOINT)

print("\n[2] bash 语法")
r = subprocess.run(["bash", "-n", str(ROOT / "entrypoint.sh")],
                   capture_output=True, text=True)
check("bash -n 通过", r.returncode == 0, r.stderr[:200])

SEG = extract_link_segment()

print("\n[3] 复刻故障现场 → 跑修复逻辑 → 验证无环")

tmp = Path(tempfile.mkdtemp())
try:
    a, b = tmp / "autobuddy", tmp / "wb-switch"
    a.mkdir(); b.mkdir()

    # 现场：B 侧悬空链接（指向 A 的 wal，实体已被 SQLite 删除）
    (a / "accounts.json").write_text("{}")
    (b / "token_stats.db-wal").symlink_to(a / "token_stats.db-wal")   # 悬空
    # A 侧有 wal/shm 实体（SQLite 写的）
    (a / "token_stats.db-wal").write_text("wal")
    (a / "token_stats.db-shm").write_text("shm")

    r2 = run_link_logic(SEG, a, b)
    check("修复逻辑执行成功", r2.returncode == 0, r2.stderr[:200])
    check("无循环 / 无悬空链接", not find_bad_links(a, b))

    # 普通实体仍双向传播（功能不回退）。
    # ⚠️ os.path.realpath 返回 str，必须包成 Path 且两侧均 realpath（兼容 macOS /var -> /private/var）。
    check("普通文件仍双向传播（accounts.json → .wb-switch）",
          (b / "accounts.json").is_symlink()
          and Path(os.path.realpath(b / "accounts.json")) == Path(os.path.realpath(a / "accounts.json")))

    print("\n[4] SQLite 边车不跨目录链接（干净场景）")

    a2, b2 = tmp / "a2", tmp / "b2"
    a2.mkdir(); b2.mkdir()
    (a2 / "token_stats.db").write_text("db")
    (a2 / "token_stats.db-wal").write_text("wal")
    (a2 / "token_stats.db-shm").write_text("shm")
    run_link_logic(SEG, a2, b2)
    check("边车不跨目录（B 侧无 -wal / -shm）",
          not (b2 / "token_stats.db-wal").exists()
          and not (b2 / "token_stats.db-shm").exists())
    check("主库仍正常传播（功能不回退）",
          (b2 / "token_stats.db").is_symlink()
          and Path(os.path.realpath(b2 / "token_stats.db")) == Path(os.path.realpath(a2 / "token_stats.db")))

    print("\n[5] 幂等性")

    before = {p.name: os.readlink(p) if p.is_symlink() else "file"
              for p in list(a.iterdir()) + list(b.iterdir())}
    run_link_logic(SEG, a, b)
    after = {p.name: os.readlink(p) if p.is_symlink() else "file"
             for p in list(a.iterdir()) + list(b.iterdir())}
    check("二次执行结果一致（幂等）", before == after, f"{before} vs {after}")

    print("\n[6] 真实故障形态：互指清理后重启，不再重建互指")

    a3, b3 = tmp / "a3", tmp / "b3"
    a3.mkdir(); b3.mkdir()
    (a3 / "token_stats.db").write_text("db")
    # 制造生产中的双向互指
    (a3 / "token_stats.db-wal").symlink_to(b3 / "token_stats.db-wal")
    (b3 / "token_stats.db-wal").symlink_to(a3 / "token_stats.db-wal")
    check("前置条件确为互指（否则场景无效）",
          not Path(os.path.realpath(a3 / "token_stats.db-wal")).exists())
    # 运维清理互指（与线上急救动作一致），再跑 entrypoint 逻辑
    (a3 / "token_stats.db-wal").unlink()
    (b3 / "token_stats.db-wal").unlink()
    run_link_logic(SEG, a3, b3)
    check("重启后不再重建互指", not find_bad_links(a3, b3))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
