"""刷新令牌的防护与熔断（refresh 防复发）。

背景（2026-10-17 批量失效事件）：一天之内 17/24 个 codebuddy.cn 账号
同时需要重新登录。排查结论是**上游 session 被外部批量重置**，不是本系统
定时任务触发 —— 本系统的 ``_refresh_one_token`` 只会在人工点「刷新 Token」
时被调用，没有批量自动刷新。但这次事件暴露了四个防护空白，本模块补上：

1. **加锁 + 单飞**：同一账号的刷新请求并发时只允许一次在飞，其余共享结果，
   避免前端连点/并发把上游刷新端点打爆、或让多份响应互相覆盖写坏账号文件。

2. **失败隔离（需人工重登）**：refresh 报 ``issued before session started``
   或 ``is invalid`` 说明 RT 已在上游失效（session 被重置），重试无意义，
   标记为「需人工重登」，不再自动重试 —— 避免无效请求持续轰炸上游。

3. **批量熔断告警**：记录刷新失败事件时间线，短时间内（默认 120s）大量
   账号（默认 ≥5）刷新失败时，触发一次告警，提醒用户疑似上游批量重置。

4. **节奏限制**：单账号刷新设最小间隔（默认 5 分钟）+ 每日上限
   （默认 3 次/账号/日），防止误操作或脚本频繁刷新。

设计约束：
- 状态独立存 ``refresh_state.json``，**不污染账号对象**。账号对象是账号池
  的核心数据结构，写额外字段会跟着 ``_save_accounts`` 落到官方那份文件里，
  增加官方前端解析出错的概率；这里独立存，边界清晰。
- 与 ``account_policy`` 的停用语义**刻意分开**：停用 = 不参与轮询（路由
  据此跳号），「需人工重登」= 只是提示，账号可能仍能用（AT 未过期时）。
- 所有失败都**不影响刷新主流程**：本模块只做记录与提示，永不抛出异常。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
STATE_FILE = DATA_DIR / "refresh_state.json"

# ---- 可配参数 ----
# 单飞：同一账号刷新锁的默认超时（秒）。超时后允许新请求接管，避免死锁。
FLIGHT_TIMEOUT = float(os.getenv("AB_RG_FLIGHT_TIMEOUT", "30") or 30)
# 节奏限制：单账号两次刷新之间的最小间隔（秒）。
MIN_INTERVAL = float(os.getenv("AB_RG_MIN_INTERVAL", "300") or 300)
# 节奏限制：单账号每日刷新上限（次）。
DAILY_LIMIT = int(os.getenv("AB_RG_DAILY_LIMIT", "3") or 3)
# 熔断告警：时间窗口（秒）与阈值（失败账号数）。
ALERT_WINDOW = float(os.getenv("AB_RG_ALERT_WINDOW", "120") or 120)
ALERT_THRESHOLD = int(os.getenv("AB_RG_ALERT_THRESHOLD", "5") or 5)
# 告警去重：两次熔断告警之间的最小间隔（秒），防止持续失败时刷屏。
ALERT_COOLDOWN = float(os.getenv("AB_RG_ALERT_COOLDOWN", "1800") or 1800)

# 刷新失败时命中即判定「需人工重登」的上游原话关键词（小写匹配）。
_RELOGIN_MARKERS = (
    "issued before session started",
    "is invalid",
)

_LOCK = threading.Lock()

# 进程内单飞标记：account_id -> 在飞时间戳。跨进程不共享，但容器内是单进程，
# 足够覆盖「前端连点 / 并发请求」场景。
_FLIGHT: Dict[str, float] = {}


def _now() -> float:
    return time.time()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load() -> Dict[str, Any]:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(STATE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[refresh_guard] save failed: {e}")


# ---------------------------------------------------------------------------
# ① 加锁 + 单飞
# ---------------------------------------------------------------------------

def try_acquire(account_id: str) -> bool:
    """尝试取得某账号的刷新权（单飞）。返回 True 表示可以刷新。

    已在飞且未超时 → False（调用方应直接返回「刷新中，请稍候」）；
    未在飞或已超时 → 记录当前时间戳并返回 True。
    """
    acc = str(account_id)
    with _LOCK:
        started = _FLIGHT.get(acc)
        now = _now()
        if started is not None and (now - started) < FLIGHT_TIMEOUT:
            return False
        _FLIGHT[acc] = now
        return True


def release(account_id: str) -> None:
    """刷新结束（无论成败）后释放单飞标记。"""
    acc = str(account_id)
    with _LOCK:
        _FLIGHT.pop(acc, None)


# ---------------------------------------------------------------------------
# ④ 节奏限制
# ---------------------------------------------------------------------------

def _day_bucket(now: float) -> str:
    """按本地日期归桶（用于每日上限）。"""
    import datetime
    return datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def check_rate(account_id: str) -> Optional[str]:
    """节奏限制检查。返回 None 表示放行；否则返回拒绝原因（中文）。

    顺序：先看最小间隔，再看每日上限。
    """
    acc = str(account_id)
    state = _load()
    acc_state = state.get(acc) or {}
    now = _now()
    last = acc_state.get("lastRefreshAt")
    if last:
        try:
            if now - float(last) < MIN_INTERVAL:
                return "刷新过于频繁，请稍后再试"
        except (TypeError, ValueError):
            pass
    # 每日上限：按「刷新成功」计数（失败不占额度，否则一次故障就把号锁死）。
    daily = acc_state.get("daily") or {}
    today = _day_bucket(now)
    cnt = daily.get(today, 0)
    if isinstance(cnt, int) and cnt >= DAILY_LIMIT:
        return "今日刷新次数已达上限，请明天再试"
    return None


def _mark_refreshed(account_id: str, success: bool) -> None:
    """记录一次刷新（成功才累计每日计数；时间戳两者都记）。"""
    acc = str(account_id)
    state = _load()
    acc_state = state.get(acc) or {}
    now = _now()
    acc_state["lastRefreshAt"] = now
    if success:
        daily = acc_state.get("daily") or {}
        today = _day_bucket(now)
        daily[today] = int(daily.get(today, 0)) + 1
        # 只保留最近 3 天的桶，避免状态文件无限膨胀。
        acc_state["daily"] = dict(list(daily.items())[-3:])
    state[acc] = acc_state
    _save(state)


# ---------------------------------------------------------------------------
# ② 失败隔离（需人工重登）
# ---------------------------------------------------------------------------

def _looks_relogin(err: str) -> bool:
    """判断刷新失败是否命中「RT 已失效需人工重登」的语义。"""
    low = (err or "").lower()
    return any(m in low for m in _RELOGIN_MARKERS)


def record_failure(account_id: str, err: str) -> Dict[str, Any]:
    """记录一次刷新失败。返回本次是否触发了「需人工重登」标记。

    同时把失败事件喂给熔断告警（③）。本函数永不抛异常。
    """
    acc = str(account_id)
    state = _load()
    acc_state = state.get(acc) or {}
    now = _now()

    needs_relogin = _looks_relogin(err)
    if needs_relogin:
        acc_state["needsRelogin"] = True
        acc_state["reloginReason"] = (err or "")[:200]
        acc_state["reloginAt"] = _now_ms()
    acc_state["lastError"] = (err or "")[:200]
    acc_state["lastErrorAt"] = _now_ms()
    state[acc] = acc_state

    # ③ 熔断告警事件时间线
    events = state.get("_failureEvents") or []
    events.append(now)
    # 只保留窗口内的，避免无限增长
    events = [t for t in events if now - t <= ALERT_WINDOW]
    state["_failureEvents"] = events
    _save(state)

    # 触发熔断告警（带去重）
    triggered = _maybe_alert(events, state)
    return {"needsRelogin": needs_relogin, "alertTriggered": triggered}


def record_success(account_id: str) -> None:
    """刷新成功：清除「需人工重登」标记与失败记录，累计节奏计数。"""
    acc = str(account_id)
    state = _load()
    acc_state = state.get(acc) or {}
    acc_state.pop("needsRelogin", None)
    acc_state.pop("reloginReason", None)
    acc_state.pop("reloginAt", None)
    acc_state.pop("lastError", None)
    acc_state.pop("lastErrorAt", None)
    state[acc] = acc_state
    _save(state)
    _mark_refreshed(acc, success=True)


def needs_relogin(account_id: str) -> bool:
    """查询某账号是否被标记为「需人工重登」。"""
    acc = str(account_id)
    state = _load()
    return bool((state.get(acc) or {}).get("needsRelogin"))


def needs_relogin_ids() -> Set[str]:
    """所有被标记「需人工重登」的账号 id 集合。"""
    state = _load()
    out: Set[str] = set()
    for acc, acc_state in state.items():
        if isinstance(acc_state, dict) and acc_state.get("needsRelogin"):
            out.add(acc)
    return out


def clear_relogin(account_id: str) -> None:
    """清除「需人工重登」标记（用户已重新登录后调用）。"""
    acc = str(account_id)
    state = _load()
    acc_state = state.get(acc) or {}
    acc_state.pop("needsRelogin", None)
    acc_state.pop("reloginReason", None)
    acc_state.pop("reloginAt", None)
    if acc_state:
        state[acc] = acc_state
    else:
        state.pop(acc, None)
    _save(state)


# ---------------------------------------------------------------------------
# ③ 批量熔断告警
# ---------------------------------------------------------------------------

def _maybe_alert(events: List[float], state: Dict[str, Any]) -> bool:
    """窗口内失败账号数达阈值且距上次告警足够久 → 触发一次告警。

    通过回调注入 notify，避免本模块硬依赖 notify（保持可测）。返回是否触发。
    """
    if len(events) < ALERT_THRESHOLD:
        return False
    now = _now()
    last_alert = state.get("_lastAlertAt")
    if last_alert and (now - float(last_alert)) < ALERT_COOLDOWN:
        return False
    # 尝试推送告警（回调在 main.py 里注入，避免循环 import）
    cb = _get_alert_callback()
    if cb is None:
        return False
    try:
        cb(len(events))
    except Exception as e:
        print(f"[refresh_guard] alert callback failed: {e}")
        return False
    state["_lastAlertAt"] = now
    _save(state)
    return True


_ALERT_CALLBACK = None


def _get_alert_callback():
    global _ALERT_CALLBACK
    return _ALERT_CALLBACK


def set_alert_callback(cb) -> None:
    """注入告警回调：``cb(failed_count)``。由 main.py 在启动时调用。"""
    global _ALERT_CALLBACK
    _ALERT_CALLBACK = cb


# ---------------------------------------------------------------------------
# 查询接口（前端展示用）
# ---------------------------------------------------------------------------

def snapshot() -> Dict[str, Any]:
    """给前端/接口的完整状态快照（已剔除内部时间线）。"""
    state = _load()
    out: Dict[str, Any] = {}
    for acc, acc_state in state.items():
        if not isinstance(acc_state, dict):
            continue
        if acc.startswith("_"):
            continue
        out[acc] = {
            "needsRelogin": bool(acc_state.get("needsRelogin")),
            "reloginReason": acc_state.get("reloginReason"),
            "reloginAt": acc_state.get("reloginAt"),
            "lastError": acc_state.get("lastError"),
            "lastErrorAt": acc_state.get("lastErrorAt"),
            "lastRefreshAt": acc_state.get("lastRefreshAt"),
            "daily": acc_state.get("daily"),
        }
    return out
