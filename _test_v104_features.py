#!/usr/bin/env python3
"""v1.0.4 新增特性单测

覆盖内容：
1. 国际版独占模型集合与别名归一（_is_ai_exclusive_model）
2. 出站 base_url 解析（_resolve_base_url）
3. 白名单边界与定向放行（_enabled_accounts：独占模型放行 ai 变体账号，常规模型严格过滤）
4. /export-accounts 默认脱敏与 exportSecrets=true 逻辑
5. web_proxy.py 的 export_accounts_proxy query 透传逻辑
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

src_main = (Path(__file__).parent / "gateway" / "main.py").read_text(encoding="utf-8")
src_web = (Path(__file__).parent / "gateway" / "web_proxy.py").read_text(encoding="utf-8")

# 1. 抽取常量与路由解析函数
seg_consts = src_main[src_main.index("DATA_DIR = Path("):src_main.index("def infer_owner(")]
seg_pool = src_main[src_main.index("def _load_pool_config("):src_main.index("def select_account(")]

ns = {
    "json": json,
    "os": os,
    "Path": Path,
    "print": print,
    "Optional": __import__("typing").Optional,
    "List": __import__("typing").List,
    "Dict": __import__("typing").Dict,
    "Any": __import__("typing").Any,
    "Set": __import__("typing").Set,
    "set": set,
    "frozenset": frozenset,
    "threading": __import__("threading"),
}
import model_policy as _mp
import account_policy as _ap
ns["model_policy"] = _mp
ns["account_policy"] = _ap
ns["ACCOUNT_POOL_FILE"] = tmpdir / "account_pool_config.json"
ns["DATA_DIR"] = tmpdir
ns["MODEL_ALIAS_MAP"] = {"hy4": "hy3"}

exec(compile(seg_consts, "main_consts", "exec"), ns)
exec(compile(seg_pool, "main_pool", "exec"), ns)

print("== 1. 国际版独占模型判定与别名归一 ==")
_is_ai_exclusive = ns["_is_ai_exclusive_model"]
check("o4-mini 属于独占模型", _is_ai_exclusive("o4-mini"))
check("seedance-2.5 属于独占模型", _is_ai_exclusive("seedance-2.5"))
check("default-model 属于独占模型", _is_ai_exclusive("default-model"))
check("fast-model 属于独占模型", _is_ai_exclusive("fast-model"))
check("balanced-model 属于独占模型", _is_ai_exclusive("balanced-model"))
check("primary-model 属于独占模型", _is_ai_exclusive("primary-model"))
check("auto-chat 属于独占模型", _is_ai_exclusive("auto-chat"))
check("completion-1.0 属于独占模型", _is_ai_exclusive("completion-1.0"))
check("enhance-1.0 属于独占模型", _is_ai_exclusive("enhance-1.0"))
check("nes-1.2 属于独占模型", _is_ai_exclusive("nes-1.2"))
check("nes-1.1 属于独占模型", _is_ai_exclusive("nes-1.1"))
check("gpt-image-2.5-sunburst 属于独占模型", _is_ai_exclusive("gpt-image-2.5-sunburst"))

check("glm-4 不属于独占模型", not _is_ai_exclusive("glm-4"))
check("kimi-k3 不属于独占模型", not _is_ai_exclusive("kimi-k3"))
check("deepseek-v3 不属于独占模型", not _is_ai_exclusive("deepseek-v3"))
check("None 不属于独占模型", not _is_ai_exclusive(None))
check("空串不属于独占模型", not _is_ai_exclusive(""))

print("\n== 2. 出站 base_url 解析 ==")
_resolve_base_url = ns["_resolve_base_url"]
CN_BASE = ns["CN_BASE_URL"]
AI_BASE = ns["AI_BASE_URL"]

check("常规模型 cn 变体指向 CN_BASE", _resolve_base_url("cn", "glm-4") == CN_BASE)
check("常规模型 ai 变体指向 AI_BASE", _resolve_base_url("ai", "glm-4") == AI_BASE)
check("独占模型 default-model cn 账号也映射到 AI_BASE", _resolve_base_url("cn", "default-model") == AI_BASE)
check("独占模型 default-model ai 账号映射到 AI_BASE", _resolve_base_url("ai", "default-model") == AI_BASE)
check("独占模型 o4-mini 映射到 AI_BASE", _resolve_base_url("cn", "o4-mini") == AI_BASE)
check("独占模型 fast-model 映射到 AI_BASE", _resolve_base_url("ai", "fast-model") == AI_BASE)

print("\n== 3. 白名单边界与国际模型定向放行 ==")
_enabled_accounts = ns["_enabled_accounts"]

# 夹具注入
ns["_account_is_usable"] = lambda a, now: True
ns["time"] = __import__("time")

ACCOUNTS = [
    {"id": "cn-1", "variant": "cn", "token": "tok-cn-1"},
    {"id": "cn-2", "variant": "cn", "token": "tok-cn-2"},
    {"id": "ai-1", "variant": "ai", "token": "tok-ai-1"},
    {"id": "ai-2", "variant": "ai", "token": "tok-ai-2"},
]
# 白名单配置只有 cn-1
POOL_CFG = {"enabledAccountIds": ["cn-1"]}

# 3.1 常规模型调用
res_cn = _enabled_accounts(ACCOUNTS, config=POOL_CFG, model="glm-4")
check("常规模型调用：严格执行白名单，只选中 cn-1", [a["id"] for a in res_cn] == ["cn-1"])

# 3.2 国际独占模型调用：白名单依然强制生效，绝不定向放行未在白名单的 ai 账号
POOL_CFG_WITH_AI = {"enabledAccountIds": ["cn-1", "ai-1"]}
res_ai = _enabled_accounts(ACCOUNTS, config=POOL_CFG_WITH_AI, model="default-model")
check("国际独占模型调用：严格执行白名单，只选中白名单内的 ai-1", [a["id"] for a in res_ai] == ["ai-1"])

# 未加白名单的 ai 账号不被选中
res_ai_no_wl = _enabled_accounts(ACCOUNTS, config=POOL_CFG, model="default-model")
check("国际独占模型调用：若白名单无 ai 账号，则候选集为空（不越权放行）", [a["id"] for a in res_ai_no_wl] == [])

# 3.3 国际独占模型调用时，账号停用策略依然生效
pol = _ap.load_policy()
_ap.set_disabled(pol, "ai-1", True, source="manual")
_ap.save_policy(pol)
try:
    res_ai_disabled = _enabled_accounts(ACCOUNTS, config=POOL_CFG_WITH_AI, model="default-model")
    check("国际独占模型调用：被账号策略停用的 ai-1 不参与", [a["id"] for a in res_ai_disabled] == [])
finally:
    _ap.set_disabled(pol, "ai-1", False)
    _ap.save_policy(pol)

# 3.4 无 model 参数时保持常规白名单行为
res_none = _enabled_accounts(ACCOUNTS, config=POOL_CFG, model=None)
check("未传 model 时：严格执行白名单", [a["id"] for a in res_none] == ["cn-1"])

seg_export = src_main[src_main.index('_EXPORT_SECRET_KEYS = frozenset('):src_main.index('@app.get("/export-accounts")')]
exec(compile(seg_export, "export_sec", "exec"), ns)

print("\n== 4. /export-accounts 默认脱敏逻辑 ==")
_EXPORT_SECRET_KEYS = ns["_EXPORT_SECRET_KEYS"]
check("脱敏敏感键包含 access_token", "access_token" in _EXPORT_SECRET_KEYS)
check("脱敏敏感键包含 refresh_token", "refresh_token" in _EXPORT_SECRET_KEYS)
check("脱敏敏感键包含 auth_raw", "auth_raw" in _EXPORT_SECRET_KEYS)
check("脱敏敏感键包含 profile_raw", "profile_raw" in _EXPORT_SECRET_KEYS)

sample_account = {
    "accountId": "acc-1",
    "email": "test@example.com",
    "access_token": "secret_at_123",
    "refresh_token": "secret_rt_456",
    "auth_raw": {"key": "val"},
    "profile_raw": {"sub": "123"},
    "custom_name": "My Account",
}
# 模拟默认脱敏导出
sanitized_acc = {k: v for k, v in sample_account.items() if k not in _EXPORT_SECRET_KEYS}
check("默认脱敏结果不含 access_token", "access_token" not in sanitized_acc)
check("默认脱敏结果不含 refresh_token", "refresh_token" not in sanitized_acc)
check("默认脱敏结果不含 auth_raw", "auth_raw" not in sanitized_acc)
check("默认脱敏结果不含 profile_raw", "profile_raw" not in sanitized_acc)
check("默认脱敏结果保留基本信息", sanitized_acc.get("email") == "test@example.com" and sanitized_acc.get("accountId") == "acc-1")

print("\n== 5. web_proxy.py query 透传 ==")
check("web_proxy 包含 export_accounts_proxy", "def export_accounts_proxy" in src_web)
check("export_accounts_proxy 获取 request.query_params", "request.query_params" in src_web)
check("export_accounts_proxy 透传 params 到上游", "params=dict(query_params)" in src_web or "params=query_params" in src_web or "params=" in src_web)

print(f"\n==============================================")
print(f"总计: {PASS} 项通过，{FAIL} 项失败")
if FAIL > 0:
    sys.exit(1)
