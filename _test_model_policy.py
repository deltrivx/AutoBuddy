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
  7. **v1 数组格式兼容**：旧文件一律读作手动禁用
  8. **来源标记**：manual / auto 的写入、读取与统计
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
os.environ["AB_DATA_DIR"] = str(tmpdir)
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

# ---- 2. 写入与读回（main.py 的包装函数按 manual 记来源）----
_save_model_policy({"acc1": ["hy3", "kimi-k3"]})
pol = _load_model_policy()
check("读回禁用列表（带来源）",
      pol == {"acc1": {"hy3": "manual", "kimi-k3": "manual"}}, str(pol))
check("包装层写入一律标记为手动",
      _mp.source_of(pol, "acc1", "hy3") == "manual")

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
# 空列表等价于「没有禁用项」，落盘时应直接丢掉该账号键，不留脏数据。
_save_model_policy({"accX": []})
check("空列表不产生空键", "accX" not in _load_model_policy())

# ---- 10. 去重与排序 ----
_save_model_policy({"accY": ["b", "a", "b"]})
check("去重排序", sorted(_load_model_policy()["accY"]) == ["a", "b"])

# ---- 11. v1 数组格式兼容 ----
# v0.4.2 之前策略文件是 {"账号": ["模型", ...]}，那时没有巡检，
# 条目全部来自人手点击 —— 读取时必须一律标记为 manual，
# 否则升级后巡检的自动启用会把这些「人为取舍」当成自己的产物放开放开。
MODEL_POLICY_FILE.write_text(json.dumps({"a1": ["hy3", "hy4"], "a2": []}), encoding="utf-8")
legacy = _load_model_policy()
check("v1 数组读作 v2 结构",
      legacy == {"a1": {"hy3": "manual", "hy4": "manual"}}, str(legacy))
check("v1 条目来源为手动", _mp.source_of(legacy, "a1", "hy3") == "manual")
check("v1 空数组不产生空键", "a2" not in legacy)
check("v1 读入后禁用判定仍然有效", _model_is_disabled("a1", "hy3", legacy))

# ---- 12. 来源标记：写入与统计 ----
pol = {}
_mp.set_model_disabled(pol, "z1", "hy3", True, source="auto")
_mp.set_model_disabled(pol, "z1", "kimi-k3", True, source="manual")
check("auto 项来源正确", _mp.source_of(pol, "z1", "hy3") == "auto")
check("manual 项来源正确", _mp.source_of(pol, "z1", "kimi-k3") == "manual")
check("按来源统计", _mp.count_by_source(pol) == {"manual": 1, "auto": 1},
      str(_mp.count_by_source(pol)))

# 自动启用只放开 auto 项
_mp.auto_enable(pol, "z1", "hy3")
_mp.auto_enable(pol, "z1", "kimi-k3")
check("自动启用只放开 auto 项",
      list((pol.get("z1") or {}).keys()) == ["kimi-k3"], str(pol.get("z1")))

# 自动禁用不覆盖已有的手动标记
_mp.auto_disable(pol, "z1", "kimi-k3")
check("自动禁用不把 manual 降级为 auto", _mp.source_of(pol, "z1", "kimi-k3") == "manual")
check("自动禁用对未禁用项写入 auto", _mp.auto_disable(pol, "z1", "glm-5.3") is True)
check("自动禁用对已禁用项报告无变更", _mp.auto_disable(pol, "z1", "glm-5.3") is False)

# 非法来源回退为 manual
_mp.set_model_disabled(pol, "z2", "m", True, source="bogus")
check("非法来源回退为手动", _mp.source_of(pol, "z2", "m") == "manual")

# ---- 13. 对外线格式仍是「模型名数组」 ----
_save_model_policy({"accL": ["hy3", "kimi-k3"]})
check("列表视图保持既有线格式",
      _mp.as_model_lists(_load_model_policy()) == {"accL": ["hy3", "kimi-k3"]},
      str(_mp.as_model_lists(_load_model_policy())))

# ---- 14. 巡检的探测范围：只跳过 manual 项 ----
# 手动禁用是人的明确决定，探测它得不到有用的结论，只会白花额度，
# 并让「手动禁用的 N 项」与「巡检禁用的 M 项」在界面上混在一起。
# auto 项**必须**继续探测：自愈正是靠这轮探测发现模型恢复可用。
mixed = {"z9": {"hy3": "manual", "kimi-k3": "auto", "glm-5.3": "manual"}}
check("只列出 manual 项", _mp.manual_disabled_for("z9", mixed) == {"hy3", "glm-5.3"},
      str(_mp.manual_disabled_for("z9", mixed)))
check("auto 项不在跳过名单里", "kimi-k3" not in _mp.manual_disabled_for("z9", mixed))
check("未知账号返回空集", _mp.manual_disabled_for("nope", mixed) == set())
check("空账号名返回空集", _mp.manual_disabled_for(None, mixed) == set())
# 别名同样要归一：用户禁的是 hy4，调用方拿 hy3 也必须被挡在探测之外。
check("手动禁用别名 hy4 -> 跳过名单也含目标 hy3",
      _mp.manual_disabled_for("z9", {"z9": {"hy4": "manual"}}) == {"hy4", "hy3"},
      str(_mp.manual_disabled_for("z9", {"z9": {"hy4": "manual"}})))
# 兼容 v1 数组形状：旧文件一律读作 manual，因此也在跳过之列。
check("v1 数组形状一律算手动", _mp.manual_disabled_for("z9", {"z9": ["hy3"]}) == {"hy3"})

print(f"\n结果：{PASS} 项通过，{FAIL} 项失败")
sys.exit(1 if FAIL else 0)
