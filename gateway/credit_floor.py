"""积分保底：余额过低的账号不再参与「收费模型」的选号。

参考 workbuddy2api-panel（2009★）的 `pool.credit_floor`：

> 账号余额低于该值时，对**实测收费**模型不再参与选号 —— 防止收费模型把余额
> 打穿、连免费模型都 402 冷却到次日签到（最坏约 11.5 小时不可用）。

实测依据（本项目 2026-10-06）：
`credit_usage_snapshots.json` 里 31 个账号中 **6 个余额 < 100，其中几个为 0**。
余额为 0 的账号拿来跑请求，只会不断撞 402/额度耗尽 —— 白白占用候选位、
拖慢选号，还让客户端看到随机失败。

与 panel 的差异（如实说明）：
panel 能区分 tier 0（实测免费）/ tier 1（无观测）/ tier 2（实测收费），
只对 tier 2 施加保底，避免「免费模型也被拦」和「触底号学不回来」。
本网关**没有**这个观测账本，因此做成：

- 默认 `floor=0`（**关闭**），行为与现在完全一致；
- 开启后，对**已知余额低于 floor** 的账号，不再选它服务**收费模型**；
- 「免费模型」名单由 `AB_FREE_MODELS` 声明（逗号分隔，支持前缀匹配）；
  未声明的模型按收费处理 —— 宁可保守，也不让触底号继续挨打；
- **永不返回空集合**：全部触底时回退原候选集，让上游返回真实错误。

余额数据来源：`credit_usage_snapshots.json`（官方底层写的快照，
含 accountId / remaining / total / ts）。取每个账号**最新一条**。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
SNAPSHOT_FILE = DATA_DIR / "credit_usage_snapshots.json"

# 0 = 关闭（默认）
FLOOR = float(os.getenv("AB_CREDIT_FLOOR", "0") or 0)

_FREE_MODELS = tuple(
    m.strip().lower()
    for m in (os.getenv("AB_FREE_MODELS", "") or "").split(",")
    if m.strip()
)

_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"mtime": 0.0, "balances": {}}


def _latest_balances() -> Dict[str, float]:
    """读取每个账号的最新余额（带 mtime 缓存，避免每请求解析 1MB 文件）。"""
    try:
        if not SNAPSHOT_FILE.exists():
            return {}
        mtime = SNAPSHOT_FILE.stat().st_mtime
        with _LOCK:
            if _CACHE["mtime"] == mtime and _CACHE["balances"]:
                return _CACHE["balances"]
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = data if isinstance(data, list) else []
        latest: Dict[str, Any] = {}
        for x in rows:
            if not isinstance(x, dict):
                continue
            aid = str(x.get("accountId") or "")
            if not aid:
                continue
            rem = x.get("remaining")
            if not isinstance(rem, (int, float)):
                continue
            ts = float(x.get("ts") or 0)
            prev = latest.get(aid)
            if prev is None or ts >= prev[0]:
                latest[aid] = (ts, float(rem))
        out = {k: v[1] for k, v in latest.items()}
        with _LOCK:
            _CACHE["mtime"] = mtime
            _CACHE["balances"] = out
        return out
    except Exception:
        return {}


def balance_of(account_id: Optional[str]) -> Optional[float]:
    """该账号的最新余额；未知返回 None（不做任何假设）。"""
    if not account_id:
        return None
    return _latest_balances().get(str(account_id))


def is_free_model(model: Optional[str]) -> bool:
    """该模型是否在免费名单里（前缀匹配，未声明则按收费）。"""
    if not model or not _FREE_MODELS:
        return False
    m = str(model).lower()
    return any(m == f or m.startswith(f) for f in _FREE_MODELS)


def filter_by_floor(accounts: List[Dict[str, Any]], id_of,
                    model: Optional[str] = None) -> List[Dict[str, Any]]:
    """过滤掉余额触底的账号（仅对收费模型生效）。

    保守原则：
    - floor<=0 或模型免费 → 原样返回（不改变任何行为）；
    - 余额未知（没有快照）→ 保留，不因「没数据」而误杀账号；
    - 全部触底 → 回退原集合，绝不在网关层编一个「无账号」。
    """
    if not accounts:
        return accounts
    if FLOOR <= 0:
        return accounts
    if is_free_model(model):
        return accounts

    balances = _latest_balances()
    if not balances:
        return accounts

    alive = []
    for a in accounts:
        aid = str(id_of(a) or "")
        if not aid:
            alive.append(a)
            continue
        rem = balances.get(aid)
        # 余额未知 → 保留（没有证据就不罚）
        if rem is None or rem >= FLOOR:
            alive.append(a)
    return alive if alive else accounts


def snapshot() -> Dict[str, Any]:
    """当前保底视角（排查用）。"""
    balances = _latest_balances()
    below = {k: v for k, v in balances.items() if v < FLOOR} if FLOOR > 0 else {}
    return {
        "floor": FLOOR,
        "enabled": FLOOR > 0,
        "freeModels": list(_FREE_MODELS),
        "accountCount": len(balances),
        "belowFloor": below,
    }
