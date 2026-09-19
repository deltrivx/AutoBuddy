"""模型级禁用策略：账号 × 模型 维度的黑名单。

这是「哪些账号的哪些模型不参与调用」的**唯一事实来源**，由两处共同读写：

- **手动**：账号卡片上点模型标签（经控制台转发到 ``PUT /account-models/config``）；
- **自动**：模型可用性巡检探测到不可用时写入、恢复可用时移除。

两者共用同一份文件，因此「手动禁用」与「自动禁用」是同一件事 ——
关掉巡检之后，之前自动写进去的禁用项依然生效，可以继续手动增删，不需要迁移或区分来源。

存储结构：``{"<accountId>": ["<model>", ...]}``，**只记被禁用的模型**，
未列出的模型一律视为可用 —— 这样官方上新模型时天然是可用态，不需要迁移配置。

独立成模块而不是留在 main.py 里，是为了让巡检模块能直接复用，避免两处各写一份读写逻辑。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

DATA_DIR = Path(os.getenv("WB_DATA_DIR", "/data/.wb-switch"))
MODEL_POLICY_FILE = DATA_DIR / "model_policy.json"

# 模型别名 -> 上游真实模型。禁用别名必须连带挡住目标模型，
# 否则调用方换个写法就绕过了禁用。
MODEL_ALIAS_MAP: Dict[str, str] = {
    "hy4": "hy3",
    "hunyuan-4": "hy3",
    "hunyuan": "hy3",
    "deepseek-chat": "deepseek-v3",
    "kimi": "kimi-k3",
    "gpt-4o": "gpt-5.4",
    "gpt-4": "gpt-5.4",
    "gpt-4o-mini": "gpt-5.6-luna",
}


def load_policy() -> Dict[str, List[str]]:
    """读取策略；文件缺失或损坏时返回空策略，绝不抛异常。"""
    try:
        if MODEL_POLICY_FILE.exists():
            with open(MODEL_POLICY_FILE, "r", encoding="utf-8") as f:
                value = json.load(f) or {}
            if isinstance(value, dict):
                policy: Dict[str, List[str]] = {}
                for acc_id, models in value.items():
                    if isinstance(models, list):
                        policy[str(acc_id)] = sorted({str(m) for m in models if m})
                return policy
    except Exception as e:
        print(f"[model-policy] failed to load policy: {e}", flush=True)
    return {}


def save_policy(policy: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """整份重写 + 原子替换。写盘失败一律吞掉，绝不能把 API 请求打死。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MODEL_POLICY_FILE.with_name(MODEL_POLICY_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(policy, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MODEL_POLICY_FILE)
    except Exception as e:
        print(f"[model-policy] failed to persist policy: {e}", flush=True)
    return policy


def disabled_models_for(account_id: Optional[str],
                        policy: Optional[Dict[str, List[str]]] = None) -> Set[str]:
    """返回某账号被禁用的模型集合（已归一到别名目标，避免 hy4 / hy3 绕过禁用）。"""
    if not account_id:
        return set()
    pol = policy if policy is not None else load_policy()
    raw = pol.get(str(account_id)) or []
    # 用户可能禁用别名（hy4），但上游实际收到的是目标模型（hy3）。
    # 这里把两侧都归一化，保证「禁用 hy4」也能挡住直接请求 hy3 的调用方。
    targets = {MODEL_ALIAS_MAP.get(m, m) for m in raw}
    return targets | set(raw)


def model_is_disabled(account_id: Optional[str], model: Optional[str],
                      policy: Optional[Dict[str, List[str]]] = None) -> bool:
    """判断某账号的某模型是否被禁用（别名两侧都算命中）。"""
    if not model:
        return False
    disabled = disabled_models_for(account_id, policy)
    return str(model) in disabled or MODEL_ALIAS_MAP.get(str(model), str(model)) in disabled


def set_model_disabled(policy: Dict[str, List[str]], account_id: str,
                       model: str, disabled: bool) -> List[str]:
    """在策略里切换单个模型的禁用状态，返回该账号更新后的禁用列表。"""
    acc_key = str(account_id)
    current = set(policy.get(acc_key) or [])
    if disabled:
        current.add(str(model))
    else:
        current.discard(str(model))
    if current:
        policy[acc_key] = sorted(current)
    else:
        policy.pop(acc_key, None)
    return sorted(current)


def replace_account_models(policy: Dict[str, List[str]], account_id: str,
                           models: List[str]) -> List[str]:
    """整份替换某账号的禁用列表，返回更新后的列表。"""
    acc_key = str(account_id)
    current = sorted({str(m) for m in models if m})
    if current:
        policy[acc_key] = current
    else:
        policy.pop(acc_key, None)
    return current


def disabled_total(policy: Optional[Dict[str, List[str]]] = None) -> int:
    pol = policy if policy is not None else load_policy()
    return sum(len(v) for v in pol.values())
