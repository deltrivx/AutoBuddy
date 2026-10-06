"""Token 成本的等价折算。

参考 workbuddy2api-hub（646★）的 OpenRouter 价估算：

> 把请求 token 按模型价折算成等价花费，回答「这些 token 值多少钱」。
> 输入按缓存命中/未命中两档单价拆分，输出单独计价。

与 hub 的差异（如实说明）：
hub 会定期去 OpenRouter 拉全量价目表并按版本留档。本网关**不联网取价** ——
中转网关不该为了一个显示数字引入外部依赖和失败面。这里改为：

- 内置一份小价表（美元 / 每百万 token），覆盖本项目实际在用的模型；
- 未收录的模型返回 `None`，界面显示「—」而不是编一个数字；
- 可用 `AB_PRICING_JSON` 指向自定义价表覆盖/补充（不重启不生效，可接受）。

计价口径：
- `cached_tokens`（命中缓存的输入）按 `cached_input` 单价；
- 未命中输入按 `input` 单价；
- 输出按 `output` 单价；
- 三者相加即等价美元成本。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

# 美元 / 每 1M token。来源：各厂商公开定价的近似值，
# 仅用于「这些 token 大概值多少钱」的观感对比，不是计费依据。
_BUILTIN: Dict[str, Dict[str, float]] = {
    # WorkBuddy / CodeBuddy 常用
    "hy4-preview-f":      {"input": 0.30, "cached_input": 0.03, "output": 1.20},
    "hy4":                {"input": 0.30, "cached_input": 0.03, "output": 1.20},
    "deepseek-v4.1-flash": {"input": 0.10, "cached_input": 0.01, "output": 0.40},
    "deepseek-v4-pro":    {"input": 0.50, "cached_input": 0.05, "output": 1.50},
    "glm-5.3":            {"input": 0.20, "cached_input": 0.02, "output": 0.80},
    "glm-5.3-flash":      {"input": 0.05, "cached_input": 0.005, "output": 0.20},
    "glm-5.2":            {"input": 0.20, "cached_input": 0.02, "output": 0.80},
    # 常见第三方（用户可能经本网关调用）
    "gpt-5":              {"input": 1.25, "cached_input": 0.125, "output": 10.0},
    "gpt-5-mini":         {"input": 0.25, "cached_input": 0.025, "output": 2.0},
    "claude-sonnet-4":    {"input": 3.00, "cached_input": 0.30, "output": 15.0},
    "claude-opus-4":      {"input": 15.0, "cached_input": 1.50, "output": 75.0},
    "gemini-3-pro":       {"input": 1.25, "cached_input": 0.125, "output": 5.0},
    "gemini-3-flash":     {"input": 0.15, "cached_input": 0.015, "output": 0.60},
}

_OVERRIDE_PATH = os.getenv("AB_PRICING_JSON", "").strip()
_OVERRIDE: Optional[Dict[str, Dict[str, float]]] = None


def _table() -> Dict[str, Dict[str, float]]:
    global _OVERRIDE
    if _OVERRIDE_PATH:
        if _OVERRIDE is None:
            try:
                with open(Path(_OVERRIDE_PATH), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged = dict(_BUILTIN)
                    merged.update(data)
                    _OVERRIDE = merged
                else:
                    _OVERRIDE = _BUILTIN
            except Exception:
                _OVERRIDE = _BUILTIN
        return _OVERRIDE
    return _BUILTIN


def price_of(model: Optional[str]) -> Optional[Dict[str, float]]:
    """取该模型的价目；未收录返回 None（不编造）。"""
    if not model:
        return None
    t = _table()
    m = str(model).strip()
    if m in t:
        return t[m]
    # 带渠道后缀的名字向基准模型继承（如 hy4-preview-f:cn）
    base = m.split(":")[0].strip()
    if base in t:
        return t[base]
    # 前缀匹配兜底（如 glm-5.3-xxxx）
    for key in sorted(t, key=len, reverse=True):
        if m.startswith(key):
            return t[key]
    return None


def estimate_cost(model: Optional[str],
                  input_tokens: int = 0,
                  output_tokens: int = 0,
                  cached_tokens: int = 0) -> Optional[float]:
    """折算等价美元成本；模型未定价返回 None。"""
    p = price_of(model)
    if not p:
        return None
    try:
        tin = max(0, int(input_tokens or 0))
        tout = max(0, int(output_tokens or 0))
        tcached = max(0, min(int(cached_tokens or 0), tin))
        uncached = tin - tcached
        cost = (
            uncached / 1_000_000 * float(p.get("input") or 0)
            + tcached / 1_000_000 * float(p.get("cached_input") or p.get("input") or 0)
            + tout / 1_000_000 * float(p.get("output") or 0)
        )
        return round(cost, 6)
    except Exception:
        return None


def known_models() -> Dict[str, Dict[str, float]]:
    """当前生效的价目表（排查/展示用）。"""
    return dict(_table())
