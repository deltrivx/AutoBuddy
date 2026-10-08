#!/usr/bin/env python3
"""在线配置「热 / 冷」口径回归测试（静态 AST 断言，不依赖运行时 import）。

⚠️ 为什么不 import wb_daily 跑 health_api：服务模块依赖 uvicorn，本地/CI 不一定装了，
import 会直接 ModuleNotFoundError —— 用运行时 import 会让测试在没装依赖的环境里假失败。
与 _test_webui_panels.py 一致：只解析源码结构，断言契约点，不启服务。

判定要点（v0.9.37 实测）：

- 面板配置是热的：`_scheduler_loop()` 每 5 分钟重读 `load_config()`，`_execute()` 每次也重新读；
  改 enabled / interval_hours 下一轮即按新值走，不需重启 ⇒ 不要为它做 livecfg 过度设计。
- env 常量是冷的：模块导入时读一次（实测改 env 后 SOFT_RATE_BASE 不变），必须如实标注，
  别让用户以为改了 env 也立即生效。

本测试锁定三条契约：

1. `_COLD_ENV_KEYS` 存在且为元组，元素全是大写 env 名；
2. health 返回的 dict 里有 `config_scope`，含 hot / cold_env 两组（hot 由 DEFAULT_CONFIG 派生）；
3. 配置写入是「补键不重建」—— save_config 里是 cfg.update(new_data)，不是新建干净 dict。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "gateway" / "wb_daily.py"

_ok = 0
_fail = []


def check(name, cond, extra=""):
    global _ok
    if cond:
        _ok += 1
        print("  ok   " + name)
    else:
        _fail.append(name)
        print("  FAIL " + name + (("  <- " + extra) if extra else ""))


src = SRC.read_text(encoding="utf-8")
tree = ast.parse(src)

print("[1] _COLD_ENV_KEYS 存在且为大写 env 名元组")

cold = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "_COLD_ENV_KEYS":
                cold = node.value
                break

check("_COLD_ENV_KEYS 已定义", cold is not None)
check("值为元组字面量", isinstance(cold, ast.Tuple))

if isinstance(cold, ast.Tuple):
    vals = [e.value for e in cold.elts if isinstance(e, ast.Constant)]
    check("非空", len(vals) > 0, str(vals))
    check("元素全为大写 env 名", all(v.isupper() for v in vals), str(vals))

print()
print("[2] health 返回 config_scope（hot / cold_env）")

found = False
hot_ok = False
cold_ok = False
for node in ast.walk(tree):
    if not isinstance(node, ast.Dict):
        continue
    # hot / cold_env 在 config_scope 的**内层** dict 里，不是同一层
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and k.value == "config_scope":
            found = True
            if isinstance(v, ast.Dict):
                for kk in v.keys:
                    if isinstance(kk, ast.Constant):
                        if kk.value == "hot":
                            hot_ok = True
                        if kk.value == "cold_env":
                            cold_ok = True

check("health 返回含 config_scope", found)
check("config_scope 含 hot", hot_ok)
check("config_scope 含 cold_env", cold_ok)

# hot 应由 DEFAULT_CONFIG 派生（面板可改的那批键）
derived = "sorted(DEFAULT_CONFIG.keys())" in src
check("hot 由 DEFAULT_CONFIG 派生（面板项）", derived)

print()
print("[3] 配置写入是「补键不重建」")

save_fn = None
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "save_config":
        save_fn = node

check("save_config 存在", save_fn is not None)

if save_fn:
    has_update = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "update"
        for n in ast.walk(save_fn))
    check("save_config 内调用了 update（补键）", has_update)
    has_clean_dict = any(
        isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)
        and len(n.value.keys) == 0
        for n in ast.walk(save_fn))
    check("未用空 dict 重建（不整字典重写）", not has_clean_dict)

print()
print("=" * 52)
msg = "通过 " + str(_ok) + " 项" + (("，失败 " + str(len(_fail)) + " 项：" + str(_fail)) if _fail else "，全部通过")
print(msg)
raise SystemExit(1 if _fail else 0)
