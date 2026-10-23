#!/usr/bin/env python3
"""模型自动发现回归测试（2026-10-13 修复：发现通道全断）。

根因实证：
- `_discover_from_token_stats()` 读 `token_stats_logs.json`，该文件 v0.9.29
  迁 SQLite 时已删除 → 恒返回空
- `_discover_from_official_usage()` 读 `official_usage_cache.json`，9-24 起停更
- `_catalog_signature()` 也盯着已删除的 JSON（mtime 恒 0）→ 即使发现函数修好，
  缓存也永不失效
三重叠加 ⇒ 新模型永远进不了 /v1/models（实测账本 27+ 模型，目录停在 9 月）。
"""
import ast
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "gateway" / "main.py").read_text(encoding="utf-8")
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

print("[1] 发现通道改读 SQLite")
check("_discover_from_token_stats 读 token_stats.db",
      'db_file = DATA_DIR / "token_stats.db"' in SRC)
check("DISTINCT model 查询",
      "SELECT DISTINCT model FROM usage_logs" in SRC)
check("老 JSON 仍作回退（兼容旧部署）",
      'log_file = DATA_DIR / "token_stats_logs.json"' in SRC)
check("只读连接（mode=ro，不干扰写入）", "mode=ro" in SRC)

print("\n[2] 缓存签名纳入 SQLite")
check("_catalog_signature 盯 token_stats.db",
      '"token_stats.db"' in SRC)
check("签名列表含四个文件（含 /v3/config 动态目录）",
      bool(re.search(r'\("official_usage_cache\.json", "token_stats\.db", "upstream_models\.json", "token_stats_logs\.json"\)', SRC)))

print("\n[3] AST 验证（真实解析，非字符串碰运气）")
tree = ast.parse(SRC)
fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
check("_discover_from_token_stats 存在", "_discover_from_token_stats" in fns)
check("_catalog_signature 存在", "_catalog_signature" in fns)
check("get_model_catalog 仍引用两个发现函数",
      all(f"_{name}(" in SRC for name in ("discover_from_official_usage", "discover_from_token_stats")))

print(f"\n{'='*52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
