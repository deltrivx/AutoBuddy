#!/usr/bin/env python3
"""验证「模型感知选账号」：禁用了某模型的账号必须在轮询中被跳过。

直接复用 main.py 的 select_account，只把账号来源 / 池配置 / 策略文件替换成测试夹具。
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


tmp = Path(tempfile.mkdtemp())
os.environ["AB_DATA_DIR"] = str(tmp)
# 与 _test_model_policy.py 一致：把 gateway/ 加进 sys.path，
# 否则 `import model_policy`（main.py 已把策略读写抽到该模块）会 ModuleNotFoundError。
sys.path.insert(0, str(Path(__file__).parent / "gateway"))

src = (Path(__file__).parent / "gateway" / "main.py").read_text(encoding="utf-8")

# 抽取 DATA_DIR 常量区 + 策略函数 + 池配置函数 + select_account
seg1 = src[src.index("DATA_DIR = Path("):src.index("def infer_owner(")]
seg_policy = src[src.index("def _load_model_policy("):src.index("def _account_id(")]
seg_pool = src[src.index("def _load_pool_config("):src.index("def select_account(")]
seg_select = src[src.index("def select_account("):src.index('@app.get("/account-pool/status")')]

ns = {
    "json": json, "os": os, "Path": Path, "print": print,
    "Optional": __import__("typing").Optional,
    "List": __import__("typing").List,
    "Dict": __import__("typing").Dict,
    "Any": __import__("typing").Any,
    "threading": __import__("threading"),
    "collections": __import__("collections"),
    "time": __import__("time"),
    "Counter": __import__("collections").Counter,
}
# main.py 已把策略读写抽到 model_policy 模块（巡检与手动禁用共用），
# 账号级停用又抽到 account_policy（账号池合成启用集合时会读它）。
# 切片执行时这两处都是顶层引用，必须一并注入命名空间 ——
# 少注一个的表现是 NameError，且只在跑到那条分支时才炸。
import model_policy as _mp  # noqa: E402
import account_policy as _ap  # noqa: E402

ns["model_policy"] = _mp
ns["account_policy"] = _ap
ns["ACCOUNT_POOL_FILE"] = tmp / "account_pool_config.json"
ns["SELECTION_LOG_FILE"] = tmp / "selection_logs.json"
ns["MODEL_POLICY_FILE"] = tmp / "model_policy.json"
ns["MODEL_ALIAS_MAP"] = {"hy4": "hy3"}
ns["DATA_DIR"] = tmp

exec(compile(seg1, "consts", "exec"), ns)
exec(compile(seg_policy, "policy", "exec"), ns)
exec(compile(seg_pool, "pool", "exec"), ns)

# 夹具：3 个账号，全部可用
ACCOUNTS = [
    {"id": "a1", "nickname": "Acc1", "access_token": "t1", "variant": "ai"},
    {"id": "a2", "nickname": "Acc2", "access_token": "t2", "variant": "ai"},
    {"id": "a3", "nickname": "Acc3", "access_token": "t3", "variant": "ai"},
]
ns["_load_accounts"] = lambda: ACCOUNTS
ns["_account_is_usable"] = lambda a, now: True
ns["_ACCOUNT_POOL_RUNTIME"] = {"next_index": 0, "last_selected_id": None, "last_selected_source": None}
ns["_SELECTION_LOCK"] = __import__("threading").Lock()
ns["_SELECTION_LOG"] = []
ns["_save_selection_log_locked"] = lambda: None
ns["_account_label"] = lambda a: a.get("nickname")

exec(compile(seg_select, "select", "exec"), ns)
select_account = ns["select_account"]

print("== 模型感知选账号 ==")

# 1. 无策略：轮询应覆盖三个账号
seen = set()
for _ in range(9):
    seen.add(select_account(model="hy3")["id"])
check("无禁用时轮询覆盖全部账号", seen == {"a1", "a2", "a3"}, str(seen))

# 2. a2 禁用 hy3：hy3 请求应只在 a1/a3 之间轮
ns["_load_model_policy"] = lambda: {"a2": ["hy3"]}
seen = set()
for _ in range(12):
    seen.add(select_account(model="hy3")["id"])
check("a2 禁用 hy3 后 hy3 不再落到 a2", "a2" not in seen, str(seen))
check("a2 禁用 hy3 后仍用 a1/a3", seen == {"a1", "a3"}, str(seen))

# 3. 同一账号的其他模型不受影响
seen = set()
for _ in range(9):
    seen.add(select_account(model="kimi-k3")["id"])
check("a2 的 kimi-k3 不受 hy3 禁用影响", seen == {"a1", "a2", "a3"}, str(seen))

# 4. 别名：a2 禁用 hy4（映射 hy3）也应被跳过
ns["_load_model_policy"] = lambda: {"a2": ["hy4"]}
seen = set()
for _ in range(12):
    seen.add(select_account(model="hy3")["id"])
check("a2 禁用别名 hy4 同样挡住 hy3 请求", "a2" not in seen, str(seen))

# 5. 全部账号都禁用该模型 -> 回退，不返回 None
ns["_load_model_policy"] = lambda: {"a1": ["hy3"], "a2": ["hy3"], "a3": ["hy3"]}
acc = select_account(model="hy3")
check("全禁用时回退而非 None", acc is not None, str(acc))

# 6. 不传 model 时行为与旧版一致（不过滤）
ns["_load_model_policy"] = lambda: {"a1": ["hy3"], "a2": ["hy3"], "a3": ["hy3"]}
seen = set()
for _ in range(9):
    seen.add(select_account()["id"])
check("不传 model 时不过滤", seen == {"a1", "a2", "a3"}, str(seen))

# 7. 指定账号但该账号禁用了该模型 -> 返回 None（走 409 提示）
ns["_load_model_policy"] = lambda: {"a2": ["hy3"]}
check("显式指定被禁用的组合返回 None", select_account("a2", model="hy3") is None)

# 8. 账号级停用：被停用的账号不参与自动轮询。
# 这条最容易被漏 —— 账号策略读的是独立文件，模型策略被 monkeypatch 掉了，
# 如果 _enabled_accounts 忘了接账号策略，上面的用例全都会照常通过。
_ap.save_policy({})
pol = _ap.load_policy()
_ap.set_disabled(pol, "a2", True, _ap.SOURCE_MANUAL)
_ap.save_policy(pol)
seen = set()
for _ in range(12):
    seen.add(select_account()["id"])
check("账号级停用后不参与自动轮询", "a2" not in seen, str(seen))

# 9. 被停用的账号仍可被显式指定（人工兜底场景：就想用这个账号）。
#    停用是「轮询时不选它」，不是「禁止调用」。
acc = select_account("a2")
check("显式指定的账号不受停用影响", acc is not None and acc["id"] == "a2", str(acc))

# 10. 全部账号都被停用时回退，不返回 None
for _id in ("a1", "a2", "a3"):
    p = _ap.load_policy()
    _ap.set_disabled(p, _id, True, _ap.SOURCE_MANUAL)
    _ap.save_policy(p)
acc = select_account()
check("全部停用时回退而非 None", acc is not None, str(acc))

# 11. 清空策略后恢复可轮询（停用是双向的）
_ap.save_policy({})
seen = set()
for _ in range(12):
    seen.add(select_account()["id"])
check("解除停用后重新参与轮询", "a2" in seen, str(seen))

print(f"\n结果：{PASS} 项通过，{FAIL} 项失败")
sys.exit(1 if FAIL else 0)
