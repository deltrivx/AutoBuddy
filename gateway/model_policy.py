"""模型级禁用策略：账号 × 模型 维度的黑名单。

这是「哪些账号的哪些模型不参与调用」的**唯一事实来源**，由两处共同读写：

- **手动**：账号卡片上点模型标签（经控制台转发到 ``PUT /account-models/config``）；
- **自动**：模型可用性巡检探测到不可用时写入、恢复可用时移除。

两者共用同一份文件，因此「关掉巡检之后手动禁用依然生效」是天然成立的 ——
不需要把自动写入的禁用项再迁移一遍。

来源标记
--------
每个禁用项都带一个来源：``manual``（人点的）或 ``auto``（巡检写的）。这不是装饰，
而是让两种意图**互不侵犯**：

- 自动巡检发现某个 ``auto`` 项恢复可用时，会移除它（自愈）；
- 但**永远不会**移除 ``manual`` 项 —— 手动禁用代表人的决定，常见于「这些模型
  能跑但太贵，别用」。若不区分来源，自愈会把这类控成本配置一并放开。

存储结构
--------
::

    {
      "version": 2,
      "disabled": {
        "<accountId>": { "<model>": "manual" | "auto" }
      }
    }

**只记被禁用的模型**，未列出的模型一律视为可用 —— 这样官方上新模型时天然是可用态，
不需要迁移配置。

旧版（v1）是 ``{"<accountId>": ["<model>", ...]}``，读取时自动识别并一律标记为
``manual``：v1 时代巡检尚未存在，那些条目全部来自人手点击。

独立成模块而不是留在 main.py 里，是为了让巡检模块能直接复用，避免两处各写一份读写逻辑。
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
MODEL_POLICY_FILE = DATA_DIR / "model_policy.json"

# 来源标记。manual = 人点的，auto = 巡检写的。
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"
VALID_SOURCES = (SOURCE_MANUAL, SOURCE_AUTO)

# 落盘格式版本。v1（无 version 键、值为数组）读取时兼容并归一为 v2。
POLICY_VERSION = 2

# 账号 -> {模型: 来源}。函数式别名，便于阅读与类型提示。
Policy = Dict[str, Dict[str, str]]

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


def _coerce_entry(raw: Any) -> Optional[Dict[str, str]]:
    """把「某个账号的值」归一成 ``{模型: 来源}``。无法识别时返回 None。

    两种输入都要接受，因为文件里可能同时存在新旧两种形状：

    - ``{"hy3": "auto"}``（v2）—— 也可能是 ``{"hy3": {"source": "auto"}}``，宽松接受
    - ``["hy3", "kimi-k3"]``（v1）—— 一律记为 ``manual``
    """
    if isinstance(raw, dict):
        out: Dict[str, str] = {}
        for model, source in raw.items():
            if not model:
                continue
            if isinstance(source, dict):
                source = source.get("source")
            text = str(source or SOURCE_MANUAL)
            out[str(model)] = text if text in VALID_SOURCES else SOURCE_MANUAL
        return out
    if isinstance(raw, list):
        return {str(m): SOURCE_MANUAL for m in raw if m}
    return None


def normalize_policy(policy: Any) -> Policy:
    """把任意来源的策略归一成 v2 结构，丢弃空账号键，模型名排序保证 diff 稳定。"""
    out: Policy = {}
    if isinstance(policy, dict):
        # 允许直接传入 {"version":2,"disabled":{...}} 形式的完整文件内容
        body = policy.get("disabled")
        if not isinstance(body, dict):
            body = policy
        for acc_id, entry in body.items():
            if acc_id == "version":
                continue
            coerced = _coerce_entry(entry)
            if coerced:
                out[str(acc_id)] = dict(sorted(coerced.items()))
    return out


def load_policy() -> Policy:
    """读取策略；文件缺失或损坏时返回空策略，绝不抛异常。"""
    try:
        if MODEL_POLICY_FILE.exists():
            with open(MODEL_POLICY_FILE, "r", encoding="utf-8") as f:
                value = json.load(f) or {}
            if isinstance(value, dict):
                return normalize_policy(value)
    except Exception as e:
        print(f"[model-policy] failed to load policy: {e}", flush=True)
    return {}


def save_policy(policy: Any) -> Policy:
    """整份重写 + 原子替换。写盘失败一律吞掉，绝不能把 API 请求打死。

    入参可以是 v1 的 ``{"acc": ["m"]}``（按 manual 记），也可以是 v2 的
    ``{"acc": {"m": "auto"}}``。返回值是归一后的 v2 结构。
    """
    normalized = normalize_policy(policy)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"version": POLICY_VERSION, "disabled": normalized}
        tmp = MODEL_POLICY_FILE.with_name(MODEL_POLICY_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MODEL_POLICY_FILE)
    except Exception as e:
        print(f"[model-policy] failed to persist policy: {e}", flush=True)
    return normalized


def backup_policy(suffix: str = "bak") -> Optional[Path]:
    """把当前策略文件复制一份留底，返回备份路径（无可备份内容时返回 None）。

    一次巡检可能同时改动上百个条目，判定万一有误，用户需要的是「整份回退」，
    而不是逐个在卡片上点回来。因此落盘前先留底。失败一律吞掉，绝不打断巡检。
    """
    try:
        if not MODEL_POLICY_FILE.exists():
            return None
        target = MODEL_POLICY_FILE.with_name(f"{MODEL_POLICY_FILE.name}.{suffix}")
        shutil.copyfile(MODEL_POLICY_FILE, target)
        return target
    except Exception as e:
        print(f"[model-policy] failed to back up policy: {e}", flush=True)
        return None


def as_model_lists(policy: Optional[Any] = None) -> Dict[str, List[str]]:
    """给出「只管模型名、不带来源」的视图，供对外接口保持既有线格式。"""
    pol = policy if policy is not None else load_policy()
    return {acc: sorted(entry.keys()) for acc, entry in normalize_policy(pol).items()}


def as_source_map(policy: Optional[Any] = None) -> Dict[str, Dict[str, str]]:
    """给出「账号 -> 模型 -> 来源」视图，供界面区分手动禁用与巡检禁用。"""
    pol = policy if policy is not None else load_policy()
    return normalize_policy(pol)


def disabled_models_for(account_id: Optional[str],
                        policy: Optional[Any] = None) -> Set[str]:
    """返回某账号被禁用的模型集合（已归一到别名目标，避免 hy4 / hy3 绕过禁用）。

    兼容 v1 的数组形状 —— 调用方传进来的策略可能来自测试夹具或旧接口。
    """
    if not account_id:
        return set()
    pol = policy if policy is not None else load_policy()
    entry = _coerce_entry((pol or {}).get(str(account_id))) if isinstance(pol, dict) else None
    models = set(entry or {})
    # 用户可能禁用别名（hy4），但上游实际收到的是目标模型（hy3）。
    # 这里把两侧都归一化，保证「禁用 hy4」也能挡住直接请求 hy3 的调用方。
    targets = {MODEL_ALIAS_MAP.get(m, m) for m in models}
    return targets | models


def manual_disabled_for(account_id: Optional[str],
                        policy: Optional[Any] = None) -> Set[str]:
    """返回某账号被**手动**禁用的模型集合（同样做别名归一）。

    用途只有一个：让可用性巡检把这些组合整个跳过。手动禁用是人的明确决定
    （常见于「能跑但太贵」），探测它既拿不到任何可用的结论，又白白消耗上游额度，
    还会让界面上「手动禁用的 N 项」与「巡检禁用的 M 项」互相掺杂。

    只返回 ``manual`` 项。``auto`` 项必须继续参与探测 —— 自愈正是靠这轮探测
    发现模型恢复可用、进而把它放开的。
    """
    if not account_id:
        return set()
    pol = policy if policy is not None else load_policy()
    entry = _coerce_entry((pol or {}).get(str(account_id))) if isinstance(pol, dict) else None
    models = {m for m, source in (entry or {}).items() if source == SOURCE_MANUAL}
    targets = {MODEL_ALIAS_MAP.get(m, m) for m in models}
    return targets | models


def model_is_disabled(account_id: Optional[str], model: Optional[str],
                      policy: Optional[Any] = None) -> bool:
    """判断某账号的某模型是否被禁用（别名两侧都算命中）。"""
    if not model:
        return False
    disabled = disabled_models_for(account_id, policy)
    return str(model) in disabled or MODEL_ALIAS_MAP.get(str(model), str(model)) in disabled


def source_of(policy: Any, account_id: str, model: str) -> Optional[str]:
    """返回某组合的禁用来源；未禁用时返回 None。"""
    entry = _coerce_entry((policy or {}).get(str(account_id))) if isinstance(policy, dict) else None
    return (entry or {}).get(str(model))


def set_model_disabled(policy: Policy, account_id: str, model: str,
                       disabled: bool, source: str = SOURCE_MANUAL) -> List[str]:
    """在策略里切换单个模型的禁用状态，返回该账号更新后的禁用模型列表。

    ``source`` 只在「置为禁用」时生效：人为操作写 ``manual``，巡检写 ``auto``。
    """
    if source not in VALID_SOURCES:
        source = SOURCE_MANUAL
    acc_key = str(account_id)
    entry = dict(_coerce_entry(policy.get(acc_key)) or {})
    if disabled:
        entry[str(model)] = source
    else:
        entry.pop(str(model), None)
    if entry:
        policy[acc_key] = entry
    else:
        policy.pop(acc_key, None)
    return sorted(entry.keys())


def enable_all_models(policy: Policy, account_id: str,
                      sources: Optional[List[str]] = None) -> List[str]:
    """一键恢复：移除某账号的禁用项，返回**被恢复**的模型列表。

    ``sources`` 为空表示**不分来源全部恢复**（手动 + 巡检），这是界面上
    「全部恢复」按钮的语义 —— 用户的意图是「让这些模型重新参与调用」，
    至于当初是谁禁的并不重要。

    只传 ``[SOURCE_AUTO]`` 时退化为「清掉巡检禁用」，用于需要保留人工
    决策的场景。返回值是被移除的模型名，方便界面回显「恢复了哪几个」。
    """
    acc_key = str(account_id)
    entry = _coerce_entry(policy.get(acc_key)) or {}
    if not entry:
        return []

    keep: Dict[str, str] = {}
    restored: List[str] = []
    for model, src in entry.items():
        if sources is None or src in sources:
            restored.append(str(model))
        else:
            keep[str(model)] = src

    if keep:
        policy[acc_key] = keep
    else:
        policy.pop(acc_key, None)
    return sorted(restored)


def replace_account_models(policy: Policy, account_id: str, models: List[str],
                           source: str = SOURCE_MANUAL) -> List[str]:
    if source not in VALID_SOURCES:
        source = SOURCE_MANUAL
    acc_key = str(account_id)
    entry = {str(m): source for m in models if m}
    if entry:
        policy[acc_key] = entry
    else:
        policy.pop(acc_key, None)
    return sorted(entry.keys())


def auto_disable(policy: Policy, account_id: str, model: str) -> bool:
    """巡检判定「不可用」时写入禁用。返回是否发生了变更。

    已被禁用的项**保持原有来源标记不变** —— 手动禁用的模型后来真的被上游下线时，
    它仍然属于「人的决定」，不该因为一次探测就变成可被自愈放开的 auto 项。
    """
    acc_key = str(account_id)
    entry = _coerce_entry(policy.get(acc_key)) or {}
    if str(model) in entry:
        policy[acc_key] = entry
        return False
    entry[str(model)] = SOURCE_AUTO
    policy[acc_key] = entry
    return True


def auto_enable(policy: Policy, account_id: str, model: str) -> bool:
    """巡检判定「可用」时移除禁用。返回是否发生了变更。

    **只移除 ``auto`` 项**。``manual`` 项受保护，不受巡检影响 —— 这正是来源标记
    存在的意义：自愈不能把「人为控成本而禁用」的模型悄悄放开。
    """
    acc_key = str(account_id)
    entry = _coerce_entry(policy.get(acc_key)) or {}
    if entry.get(str(model)) != SOURCE_AUTO:
        return False
    entry.pop(str(model), None)
    if entry:
        policy[acc_key] = entry
    else:
        policy.pop(acc_key, None)
    return True


def disabled_total(policy: Optional[Any] = None) -> int:
    pol = policy if policy is not None else load_policy()
    return sum(len(entry) for entry in normalize_policy(pol).values())


def count_by_source(policy: Optional[Any] = None) -> Dict[str, int]:
    """按来源统计禁用项数量，供界面显示「手动 N 项 / 巡检 M 项」。"""
    pol = policy if policy is not None else load_policy()
    counts = {SOURCE_MANUAL: 0, SOURCE_AUTO: 0}
    for entry in normalize_policy(pol).values():
        for source in entry.values():
            counts[source] = counts.get(source, 0) + 1
    return counts
