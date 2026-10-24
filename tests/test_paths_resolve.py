"""路径解析行为契约（v1.0.31 修复回归）。

2026-10-24 实测缺陷：代码里并存两种写法 —— 19 处用
``Path(os.getenv("AB_DATA_DIR", ...))`` 从环境变量读，8 处直接写死
``/data/.autobuddy`` 或 ``/data/.wb-switch``。

当前恰好 ``AB_DATA_DIR == /data/.autobuddy`` 且 ``HOME == /data``，
两边解析结果相同所以没暴露。但把挂载点从 ``/data`` 换到别处时：
  - 那 19 处正确跟着 AB_DATA_DIR 走；
  - 那 8 处仍往 /data/... 写 —— 而 /data 已不是挂载卷，
    新文件落进容器可写层，重建即丢，且不会报错。
其中 ``token_tracker`` 最严重：它用 ``TRACKER_FILE.parent`` 决定
``token_stats.db``（5MB+ 用量库）的位置，等于绕过了 AB_DATA_DIR。

本测试锁定 ``gateway/paths.py`` 的契约，防止这 8 处再退化回裸字符串。

四组断言：
1. 兜底行为：不设环境变量时解析出 /data/.autobuddy 与 /data/.wb-switch
   （必须与修复前的硬编码值逐字符相同 —— 保证行为零变化）
2. AB_DATA_DIR 单独设置时，数据目录跟随、官方目录仍由 HOME 决定
3. HOME 单独设置时，官方目录跟随、数据目录仍走兜底
4. 两者都设置时各自独立跟随

外加一组源码扫描：确认那 8 处不再出现裸 ``Path("/data/...")``。

全部走子进程隔离环境，不污染当前进程的 os.environ。
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATEWAY = ROOT / "gateway"

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


# 用子进程跑，完全隔离 os.environ —— 避免测试进程自己的环境干扰断言
_PROBE = r"""
import importlib, os, sys
sys.path.insert(0, sys.argv[1])
for k in ("AB_DATA_DIR", "HOME"):
    os.environ.pop(k, None)
env = sys.argv[2:]
for kv in env:
    k, _, v = kv.partition("=")
    os.environ[k] = v
import paths
importlib.reload(paths)
print(str(paths.get_data_dir()))
print(str(paths.get_switch_dir()))
print(str(paths.get_data_file("accounts.json")))
print(str(paths.get_switch_file("accounts.json")))
"""


def resolve(env_pairs):
    """在干净的子进程里解析路径，返回 4 元组。"""
    args = [sys.executable, "-c", _PROBE, str(GATEWAY)] + [
        f"{k}={v}" for k, v in env_pairs.items()
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None, r.stderr[-400:]
    lines = r.stdout.strip().split("\n")
    if len(lines) < 4:
        return None, f"输出行数不足: {r.stdout!r}"
    return tuple(lines[:4]), None


print("[1] 兜底行为（不设任何环境变量）")
res, err = resolve({})
check("子进程可导入 gateway/paths.py", res is not None, err or "")
if res:
    data_dir, switch_dir, data_file, switch_file = res
    check("get_data_dir() 兜底为 /data/.autobuddy",
          data_dir == "/data/.autobuddy", f"got {data_dir}")
    check("get_switch_dir() 兜底为 /data/.wb-switch",
          switch_dir == "/data/.wb-switch", f"got {switch_dir}")
    check("get_data_file 拼接正确",
          data_file == "/data/.autobuddy/accounts.json", f"got {data_file}")
    check("get_switch_file 拼接正确",
          switch_file == "/data/.wb-switch/accounts.json", f"got {switch_file}")

print("\n[2] 只设 AB_DATA_DIR")
res, err = resolve({"AB_DATA_DIR": "/config/.autobuddy"})
check("解析成功", res is not None, err or "")
if res:
    check("数据目录跟随 AB_DATA_DIR",
          res[0] == "/config/.autobuddy", f"got {res[0]}")
    check("官方目录仍由 HOME 兜底决定（不跟随 AB_DATA_DIR）",
          res[1] == "/data/.wb-switch", f"got {res[1]}")

print("\n[3] 只设 HOME")
res, err = resolve({"HOME": "/config"})
check("解析成功", res is not None, err or "")
if res:
    check("官方目录跟随 HOME",
          res[1] == "/config/.wb-switch", f"got {res[1]}")
    check("数据目录仍走兜底（不跟随 HOME）",
          res[0] == "/data/.autobuddy", f"got {res[0]}")

print("\n[4] 同时设两者")
res, err = resolve({"AB_DATA_DIR": "/config/.autobuddy", "HOME": "/config"})
check("解析成功", res is not None, err or "")
if res:
    check("数据目录跟随 AB_DATA_DIR",
          res[0] == "/config/.autobuddy", f"got {res[0]}")
    check("官方目录跟随 HOME",
          res[1] == "/config/.wb-switch", f"got {res[1]}")

print("\n[5] 生产现状复现（AB_DATA_DIR=/data/.autobuddy, HOME=/data）")
res, err = resolve({"AB_DATA_DIR": "/data/.autobuddy", "HOME": "/data"})
check("解析成功", res is not None, err or "")
if res:
    check("与修复前的硬编码值逐字符相同（行为零变化）",
          res[0] == "/data/.autobuddy" and res[1] == "/data/.wb-switch"
          and res[2] == "/data/.autobuddy/accounts.json"
          and res[3] == "/data/.wb-switch/accounts.json",
          f"got {res}")

print("\n[6] 源码扫描：8 处不得再出现裸路径")
# 只查「代码里的 Path("/data/...")」，注释行（以 # 开头）不算
_TARGETS = [
    "gateway/token_tracker.py",
    "gateway/credit_floor.py",
    "gateway/main.py",
    "gateway/wb_daily.py",
    "gateway/web_proxy.py",
]
_BARE = re.compile(r'Path\(\s*"/data/')
leaks = []
for rel in _TARGETS:
    p = ROOT / rel
    if not p.exists():
        leaks.append(f"{rel}: 文件不存在")
        continue
    for i, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
        s = line.strip()
        if s.startswith("#"):
            continue          # 注释里提到旧写法是允许的（那是变更记录）
        if _BARE.search(line):
            leaks.append(f"{rel}:{i}")
check("5 个目标文件全部存在", not any("不存在" in x for x in leaks), str(leaks[:3]))
check("不再有 Path(\"/data/...\") 裸路径", not [x for x in leaks if "不存在" not in x],
      f"残留 {[x for x in leaks if '不存在' not in x][:5]}")

print("\n[7] 工具模块存在且导出必要函数")
paths_src = (GATEWAY / "paths.py")
check("gateway/paths.py 存在", paths_src.exists())
if paths_src.exists():
    src = paths_src.read_text(encoding="utf-8")
    for fn in ("get_data_dir", "get_switch_dir", "get_home_dir",
               "get_data_file", "get_switch_file"):
        check(f"paths.py 导出 {fn}()", f"def {fn}(" in src)
    check("官方目录由 HOME 派生（不是独立 env）",
          "_SWITCH_SUBDIR" in src and "get_home_dir() / _SWITCH_SUBDIR" in src)

print("\n" + "=" * 50)
print(f"通过 {_ok} 项，失败 {len(_fail)} 项" + ("：" + str(_fail) if _fail else " ✅"))
if _fail:
    raise SystemExit(1)
