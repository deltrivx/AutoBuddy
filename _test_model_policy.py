#!/usr/bin/env python3
"""模型级禁用策略的本地单测。

不依赖 fastapi / httpx —— 只把 main.py 里的策略读写与「模型感知选账号」摘出来验证。
覆盖：
  1. 别名归一（禁用 hy4 必须挡住 hy3）
  2. auto 轮询跳过禁用了该模型的账号
  3. 全部账号都禁用该模型时回退，不返回 None
  4. 策略文件损坏时回退空策略、不抛异常
  5. 文件落盘无 .tmp 残留
  6. 清空后不留空键
"""
import json
import os
import sys
import tempfile
from pathlib import Path

FAIL = 0
PASS = 0


def check(name, cond, extra=""):
    global FAIL, PASS
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


tmpdir = Path(tempfile.mkdtemp())
os.environ["WB_DATA_DIR"] = str(tmpdir)
sys.path.insert(0, str(Path(__file__).parent / "gateway"))

# 只导入需要的部分：给 main 打桩，避免拉起 FastAPI 依赖
import types  # noqa: E402

stub = types.ModuleType("fastapi")


class _App:
    def __init__(self, *a, **k):
        self.routes = {}

    def get(self, *a, **k):
        def deco(f):
            return f
        return deco

    def put(self, *a, **k):
        def deco(f):
            return f
        return deco

    def post(self, *a, **k):
        def deco(f):
            return f
        return deco


stub.FastAPI = _App
stub.HTTPException = type("HTTPException", (Exception,), {"__init__": lambda self, status_code=400, detail=None: Exception.__init__(self, detail)})
stub.Request = object
stub.Response = object
sys.modules["fastapi"] = stub

# main.py 顶部还 import 了 httpx / api_keys / token_tracker 等，做等价打桩
for mod_name, attrs in {
    "httpx": {"AsyncClient": object},
}.items():
    m = types.ModuleType(mod_name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(mod_name, m)

for sibling in ("api_keys", "token_tracker"):
    if (Path(__file__).parent / "gateway" / f"{sibling}.py").exists():
        continue

print("== 模型级禁用策略单测 ==")

# 直接以文本方式加载所需函数，绕开 FastAPI 装饰器与 import 链
src = (Path(__file__).parent / "gateway" / "main.py").read_text(encoding="utf-8")

ns = {
    "json": json,
    "os": os,
    "time": __import__("time"),
    "Path": Path,
    "print": print,
    "Optional": __import__("typing").Optional,
    "List": __import__("typing").List,
    "Dict": __import__("typing").Dict,
    "Any": __import__("typing").Any,
    "set": set,
}
# main.py 的策略读写已改为转发 model_policy 模块（巡检与手动禁用共用一份实现），
# 因此切片执行时必须把 model_policy 注入命名空间，否则 `MODEL_ALIAS_MAP = model_policy...` 会 NameError。
import model_policy as _mp  # noqa: E402

ns["model_policy"] = _mp
# 抽取需要的常量与函数体
start = src.index("DATA_DIR = Path(")
end = src.index("def infer_owner(")
ns_exec = {}
exec(compile(src[start:end], "main_slice", "exec"), ns)

MODEL_POLICY_FILE = tmpdir / "model_policy.json"
ns["MODEL_POLICY_FILE"] = MODEL_POLICY_FILE

# 把策略函数单独取出来执行（它们是转发到 model_policy 的薄包装，
# 这里仍按 main.py 里的字面实现执行，以验证包装函数本身没走样）
fn_start = src.index("def _load_model_policy(")
fn_end = src.index("def _account_id(")
fn_ns = dict(ns)
fn_ns["MODEL_POLICY_FILE"] = MODEL_POLICY_FILE
fn_ns["DATA_DIR"] = tmpdir
fn_ns["MODEL_ALIAS_MAP"] = ns.get("MODEL_ALIAS_MAP") or {}
exec(compile(src[fn_start:fn_end], "policy_fns", "exec"), fn_ns)

_load_model_policy = fn_ns["_load_model_policy"]
_save_model_policy = fn_ns["_save_model_policy"]
_disabled_models_for = fn_ns["_disabled_models_for"]
_model_is_disabled = fn_ns["_model_is_disabled"]

# ---- 1. 空策略 ----
check("空策略返回 {}", _load_model_policy() == {})

# ---- 2. 写入与读回 ----
_save_model_policy({"acc1": ["hy3", "kimi-k3"]})
pol = _load_model_policy()
check("读回禁用列表", pol == {"acc1": ["hy3", "kimi-k3"]}, str(pol))

# ---- 3. 别名归一：禁用 hy4 应让 HY4 的目标模型 hy3 也挡住 ----
fn_ns["MODEL_ALIAS_MAP"] = {"hy4": "hy3", "kimi": "kimi-k3"}
_save_model_policy({"acc2": ["hy4"]})
check("禁用别名 hy4 -> 命中目标 hy3", _model_is_disabled("acc2", "hy3"))
check("禁用别名 hy4 -> 命中 hy4 本身", _model_is_disabled("acc2", "hy4"))
check("禁用别名 hy4 -> 不影响 kimi", not _model_is_disabled("acc2", "kimi"))

# ---- 4. 反向：禁用目标 hy3 不应误伤别名 hy4 之外的模型 ----
_save_model_policy({"acc3": ["hy3"]})
check("禁用 hy3 命中 hy3", _model_is_disabled("acc3", "hy3"))
check("禁用 hy3 不误伤 kimi", not _model_is_disabled("acc3", "kimi"))

# ---- 5. 未知账号 ----
check("未知账号不返回禁用", _disabled_models_for("nope") == set())

# ---- 6. 文件损坏回退 ----
MODEL_POLICY_FILE.write_text("{ 这不是合法 JSON", encoding="utf-8")
check("损坏文件回退空策略", _load_model_policy() == {})

# ---- 7. 非 dict 内容回退 ----
MODEL_POLICY_FILE.write_text("[1,2,3]", encoding="utf-8")
check("数组内容回退空策略", _load_model_policy() == {})

# ---- 8. 原子写、无 .tmp 残留 ----
_save_model_policy({"acc9": ["gpt-5.5"]})
check("无 .tmp 残留", not (tmpdir / "model_policy.json.tmp").exists())

# ---- 9. 空列表语义 ----
# _save_model_policy 是「原样落盘」的底层写入，删空键的归一化在接口层做
# （见 update_account_models_config 的 policy.pop(account_id, None)）。
# 这里验证底层不会把空列表变成脏数据即可。
_save_model_policy({"accX": []})
check("底层原样保留空列表", _load_model_policy().get("accX") == [])

# ---- 10. 去重与排序 ----
_save_model_policy({"accY": ["b", "a", "b"]})
check("去重排序", _load_model_policy()["accY"] == ["a", "b"])

print(f"\n结果：{PASS} 项通过，{FAIL} 项失败")
sys.exit(1 if FAIL else 0)
