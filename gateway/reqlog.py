"""请求级指标记录（脱敏）。

参考 workbuddy2api-panel 的 internal/reqlog：只归档**请求元数据**，不写提示词、
响应正文、Authorization 或其它凭证。

设计取舍：
- 有界环形缓冲（默认 200 条）保存最近请求；进程级计数保存累计量 —— 不会随时间无限增长。
- 流式响应在**流结束后**才记 total_ms，首字节耗时单独记 ttfb_ms。
  否则流式请求的耗时会严重低估（TTFB 只有 1~2s，整条流可能几十秒）。
- 只埋点 /chat/completions —— 面板轮询接口（/api/*）若也埋点会把指标刷爆，
  且对「模型调用成功率/耗时」这个问题没有价值。
"""

import collections
import os
import secrets
import threading
import time
from typing import Any, Deque, Dict, List, Optional

RECENT_MAX = int(os.getenv("AB_REQLOG_RECENT", "200") or 200)
FILL_CLIENT = os.getenv("AB_REQLOG_CLIENT", "1").strip().lower() not in ("0", "false", "no")

OUTCOME_SUCCESS = "success"
OUTCOME_HTTP_ERROR = "http_error"
OUTCOME_STREAM_ERROR = "stream_error"
OUTCOME_INTERRUPTED = "interrupted"

_lock = threading.Lock()
_recent: Deque[Dict[str, Any]] = collections.deque(maxlen=RECENT_MAX)
_pending: Dict[str, Dict[str, Any]] = {}
_totals: Dict[str, float] = {
    "total": 0,
    "success": 0,
    "http_error": 0,
    "stream_error": 0,
    "interrupted": 0,
    "sum_ms": 0.0,
    "cnt": 0,
}


def new_request_id() -> str:
    return "req-" + secrets.token_hex(8)


def begin(rid: str, **meta: Any) -> None:
    """登记一次请求的开始（只存元数据）。"""
    with _lock:
        _pending[rid] = dict(meta, rid=rid, ts=time.time())


def set_status(rid: str, status: int, ttfb_ms: float) -> None:
    """响应头已发出：记录状态码与首字节耗时。"""
    with _lock:
        ev = _pending.get(rid)
        if ev is None:
            return
        ev["status"] = int(status)
        ev["ttfb_ms"] = round(float(ttfb_ms), 1)


def done(rid: str, outcome: str) -> None:
    """请求结束：计算总耗时并归档。"""
    now = time.time()
    with _lock:
        ev = _pending.pop(rid, None)
    if ev is None:
        return
    total_ms = (now - float(ev.get("ts", now))) * 1000.0
    ev["total_ms"] = round(total_ms, 1)
    ev["outcome"] = outcome
    with _lock:
        _recent.append(ev)
        _totals["total"] += 1
        if outcome in _totals:
            _totals[outcome] += 1
        _totals["sum_ms"] += total_ms
        _totals["cnt"] += 1


def snapshot(limit: int = 50) -> Dict[str, Any]:
    """面板用的快照：汇总 + 最近请求（倒序）。"""
    with _lock:
        recent = list(_recent)
        t = dict(_totals)
    window: List[Dict[str, Any]] = recent[-limit:] if limit and limit > 0 else recent
    durations = sorted(float(e.get("total_ms") or 0.0) for e in window)

    def _pct(p: float) -> float:
        if not durations:
            return 0.0
        idx = min(len(durations) - 1, int(round((len(durations) - 1) * p)))
        return round(durations[idx], 1)

    total = int(t["total"])
    cnt = int(t["cnt"])
    return {
        "summary": {
            "total": total,
            "success": int(t["success"]),
            "httpError": int(t["http_error"]),
            "streamError": int(t["stream_error"]),
            "interrupted": int(t["interrupted"]),
            "successRate": round(t["success"] / total * 100, 1) if total else 0.0,
            "avgMs": round(t["sum_ms"] / cnt, 1) if cnt else 0.0,
            "windowCount": len(window),
            "p50Ms": _pct(0.5),
            "p95Ms": _pct(0.95),
        },
        "recent": window[::-1],
    }


def reset() -> None:
    """仅测试用：清空全部指标与待完成项。"""
    with _lock:
        _recent.clear()
        _pending.clear()
        for k in _totals:
            _totals[k] = 0
        _totals["sum_ms"] = 0.0
        _totals["cnt"] = 0
