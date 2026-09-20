"""账号级停用策略。

与 ``model_policy`` 是**同一套来源标记设计**（manual / auto），但载体独立：
模型策略的键是「账号 x 模型」，这里只有一个维度 —— 账号。

为什么不合并成一个文件：两者的生命周期与粒度都不同。模型策略会随一轮巡检
产生上百条变更，账号策略通常只有个位数条目；混在一起会让「一键回滚模型策略」
顺手把账号的停用状态也回滚掉，那不是用户想要的。

设计约束（与模型策略保持一致）：
- 两份配置都只能**减少**参与调用的账号，谁都不许把账号偷偷启用回来；
- 人工停用的账号**保持 manual 来源**，巡检不会把它改成 auto 从而自愈启用；
- 自动停用不覆盖已有的 manual 条目。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

DATA_DIR = Path(os.getenv("WB_DATA_DIR", "/data/.wb-switch"))
ACCOUNT_POLICY_FILE = DATA_DIR / "account_policy.json"

# 停用来源。manual = 人在界面上点的；auto = 可用性巡检写的。
# 分开记是为了让界面说得出「是谁停的」，也是「人工决定不被自动流程推翻」的依据。
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"
VALID_SOURCES = (SOURCE_MANUAL, SOURCE_AUTO)


def _coerce_accounts(body: Any) -> Dict[str, str]:
    """把各种历史形状归一成 ``{account_id: source}``。

    容错是必要的：这个文件可能被手改、可能来自更早的版本。
    任何解析不出来的条目一律**当手动停用**处理 —— 宁可多停一个让用户自己放开，
    也不要因为解析失败把账号悄悄放回轮询。
    """
    out: Dict[str, str] = {}
    if isinstance(body, dict):
        for key, val in body.items():
            acc_id = str(key)
            if isinstance(val, dict):
                src = str(val.get("source") or SOURCE_MANUAL)
            elif isinstance(val, str):
                src = val
            else:
                src = SOURCE_MANUAL
            out[acc_id] = src if src in VALID_SOURCES else SOURCE_MANUAL
        return out
    if isinstance(body, (list, tuple, set)):
        for item in body:
            if item is None:
                continue
            out[str(item)] = SOURCE_MANUAL
    return out


def normalize_policy(policy: Any) -> Dict[str, str]:
    """把任意输入归一成 ``{account_id: source}``。

    支持两种形状：完整文件 ``{"version": 1, "disabled": {...}}``
    与裸映射 ``{account_id: source}``。
    """
    if isinstance(policy, dict):
        body = policy.get("disabled")
        # ``disabled`` 缺失时把整个对象当裸映射；但若它是 list/tuple，
        # 那本身就是「数组形状的禁用清单」，直接交给 _coerce_accounts。
        if body is None:
            body = policy
        return _coerce_accounts(body)
    return {}


def load_policy() -> Dict[str, str]:
    if not ACCOUNT_POLICY_FILE.exists():
        return {}
    try:
        with open(ACCOUNT_POLICY_FILE, "r", encoding="utf-8") as f:
            return normalize_policy(json.load(f))
    except (OSError, ValueError):
        # 读坏了就返回空：宁可这一轮不按策略过滤（账号照常用），
        # 也不要因为一个损坏的文件让整个网关起不来。
        return {}


def save_policy(policy: Any) -> Dict[str, str]:
    normalized = normalize_policy(policy)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "disabled": normalized}
    tmp = ACCOUNT_POLICY_FILE.with_name(ACCOUNT_POLICY_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, ACCOUNT_POLICY_FILE)
    return normalized


def set_disabled(policy: Dict[str, str], account_id: str, disabled: bool,
                 source: str = SOURCE_MANUAL) -> bool:
    """切换单个账号的停用状态，返回是否发生了变更。

    ``source`` 只在「置为停用」时生效：人为操作写 ``manual``，巡检写 ``auto``。

    来源的升降规则（关键，写错会导致人工决定被自动流程推翻）：
    - 未被停用 → 写入给定来源；
    - 已停用且来源相同 → 无变更；
    - 已停用是 ``manual``、新来源是 ``auto`` → **保持 manual 不动**。
      手动停用的账号后来真的失效时，它仍然属于「人的决定」，
      不该因为一次探测就变成可被自愈启用的 auto 项；
    - 已停用是 ``auto``、新来源是 ``manual`` → 升级为 manual。
      人明确点了停用，比巡检的临时判断更持久，理应记在人头上。
    """
    if source not in VALID_SOURCES:
        source = SOURCE_MANUAL
    acc_key = str(account_id)
    if disabled:
        current = policy.get(acc_key)
        if current == source:
            return False
        if current == SOURCE_MANUAL and source == SOURCE_AUTO:
            # 保命分支：不许把人工停用降级成自动停用。
            return False
        policy[acc_key] = source
        return True
    if acc_key not in policy:
        return False
    policy.pop(acc_key, None)
    return True


def disabled_ids(policy: Optional[Any] = None) -> Set[str]:
    """当前被停用的账号 id 集合（不区分来源）。路由据此跳过这些账号。"""
    data = load_policy() if policy is None else normalize_policy(policy)
    return set(data.keys())


def as_source_map(policy: Optional[Any] = None) -> Dict[str, str]:
    """给界面用的 ``{account_id: source}``。"""
    return load_policy() if policy is None else normalize_policy(policy)


def backup_policy(suffix: str = ".bak") -> Optional[Path]:
    """落盘前留底。返回备份路径；没有原文件时返回 None。"""
    if not ACCOUNT_POLICY_FILE.exists():
        return None
    target = ACCOUNT_POLICY_FILE.with_name(ACCOUNT_POLICY_FILE.name + suffix)
    try:
        target.write_bytes(ACCOUNT_POLICY_FILE.read_bytes())
    except OSError:
        return None
    return target
