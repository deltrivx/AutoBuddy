"""上游错误的账号冷却与熔断。

参考 workbuddy2api-panel（2009★）的错误分类治理：

```
429  → 软冷却 600s 起，连续触发指数退避，封顶 2h
402  → 硬冷却至次日 04:00（余额不足，签到回血才恢复）
404  → 固定 60s 短冷却
连续失败 → 熔断（阈值 3，退避 30m×2^n，封顶 6h）
```

为什么必须有这个模块（实测依据）：

AutoBuddy 此前对 429 是**直接透传**给客户端 —— 排查中发现 73 次 429，
全部原样失败。而池里有 17 个账号：一个账号被限流，完全可以换一个；
账号余额耗尽（402）更该立刻停用，等签到回血。

没有冷却的后果：
- 被限流的账号仍在候选集里，**每个请求都可能再撞一次同样的 429**；
- 客户端看到的是随机失败，而不是「账号暂时不可用、已自动切换」。

设计要点：
- 冷却是**账号级**的，不是全局的 —— 一个号被限流不影响其他号；
- 成功请求**清零**该账号的连续失败计数（避免误伤偶发抖动）；
- 状态落盘持久化，重启不丢（容器重建很常见）；
- 过滤只作用于**自动轮询**，不拦显式指定（与账号级停用的语义一致：
  冷却是「轮询时不选它」，不是「禁止调用」）。
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
STATE_FILE = DATA_DIR / "cooldown_state.json"

# ---- 可配参数（对齐 panel 的默认值）----
SOFT_RATE_BASE = int(os.getenv("AB_CD_SOFT_BASE", "600") or 600)          # 429 软冷却基数 600s
SOFT_RATE_MAX = int(os.getenv("AB_CD_SOFT_MAX", "7200") or 7200)          # 指数退避封顶 2h
HARD_COOLDOWN_HOUR = int(os.getenv("AB_CD_HARD_HOUR", "4") or 4)          # 402 解封时刻（次日 04:00）
NOT_FOUND_COOLDOWN = int(os.getenv("AB_CD_404", "60") or 60)              # 404 固定 60s
CHANNEL_COOLDOWN = int(os.getenv("AB_CD_CHANNEL", "1800") or 1800)        # 渠道拦截 30m
BREAKER_THRESHOLD = int(os.getenv("AB_CD_BREAKER_THRESHOLD", "3") or 3)   # 连续失败熔断阈值
BREAKER_BASE = int(os.getenv("AB_CD_BREAKER_BASE", "1800") or 1800)       # 熔断基数 30m
BREAKER_MAX = int(os.getenv("AB_CD_BREAKER_MAX", "21600") or 21600)       # 熔断封顶 6h

_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


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
        print(f"[cooldown] save failed: {e}")


def _next_daily_reset(now: Optional[datetime] = None) -> float:
    """返回「次日 04:00」的时间戳（402 硬冷却的解封时刻）。

    选 04:00 而不是 00:00，是因为签到在 09/21 点才回血；
    留一点余量，避免 00:00 一过就放行但仍无额度。
    """
    now = now or datetime.now()
    target = (now + timedelta(days=1)).replace(
        hour=HARD_COOLDOWN_HOUR, minute=0, second=0, microsecond=0)
    # 若现在还没到今天的 04:00，则今天 04:00 就是解封点（跨天边界）
    today_reset = now.replace(hour=HARD_COOLDOWN_HOUR, minute=0,
                              second=0, microsecond=0)
    if now < today_reset:
        target = today_reset
    return target.timestamp()


def _classify(status_code: int, body_text: str) -> Optional[str]:
    """把上游响应归类成冷却类型；不需要冷却则返回 None。

    参考实测：
    - 额度耗尽：HTTP 429 + code=14018「Credits exhausted」（也可能 402）
    - 速率限流：HTTP 429（无额度语义）
    - 模型不存在/不支持：404 或 400 code=11102 —— 这个由模型级禁用处理，
      不在这里冷却账号（换号即可，不该惩罚账号）
    """
    if status_code == 402:
        return "hard"
    if status_code == 429:
        # 额度耗尽同样是 429，按语义判：命中额度关键词 → 硬冷却
        low = (body_text or "").lower()
        if any(k in low for k in ("insufficient balance", "credits exhausted",
                                  "quota", "额度", "余额")):
            return "hard"
        return "soft"
    if status_code == 404:
        return "not_found"
    # 渠道拦截：400 + code=11128「Illegal API invocation from an unapproved channel」
    #
    # 实测依据（2026-10-07 晚）：窗口内 2 次，全部落在「尘星途」(a944ccff…) 一个账号，
    # 同期其他账号用**完全相同**的请求形态都成功 —— 所以这是**账号级**拦截，
    # 不是请求形态/UA 问题。结论：让它退出轮询（换号即可），而不是去改请求头。
    #
    # 修复前这里返回 None（400 一律不冷却）：被拦的账号仍留在候选集里，
    # 后续请求会反复撞同一个 400。
    if status_code == 400:
        low = (body_text or "").lower()
        if "11128" in (body_text or "") and "unapproved" in low:
            return "channel"
    return None


_RESET_RX = re.compile(
    r"将在\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*UTC\+8\s*重置")
# 本地时区固定按上游口径 UTC+8 —— 上游给的就是北京时间，不要再做本机时区换算。
_CST = timezone(timedelta(hours=8))


def _parse_6004_reset(body_text: str) -> Optional[float]:
    """从 6004 的文案里解析出上游承诺的**重置墙钟**（UTC+8），返回 epoch 秒。

    实测依据（2026-10-08）：上游 429 形如

        您的使用量已超出频率限制，将在 2026-10-08 10:39:26 UTC+8 重置，
        您也可以切换其他模型继续使用。

    参考 workbuddy2api-panel 的做法：`code 6004`（**模型级**限流）带
    「将在 … 重置」时**冷却到上游重置墙钟**，而不是盲目的 2 倍指数退避。

    为什么这更好：退避是猜，重置时间是上游**明确承诺**的解封点。
    退避过短会白撞几次，过长又浪费可用额度；直接用墙钟两者都不犯。

    解析不出时返回 None，由调用方回退到指数退避 —— 宁可保守，也不错冷却。
    """
    if not body_text:
        return None
    m = _RESET_RX.search(body_text)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=_CST)
        return dt.timestamp()
    except Exception:
        return None


def record_failure(account_id: str, status_code: int, body_text: str) -> Optional[str]:
    """记录一次上游失败，返回施加的冷却类型（None = 不冷却）。"""
    if not account_id:
        return None
    kind = _classify(status_code, body_text)
    now = _now()
    with _LOCK:
        state = _load()
        acc = state.setdefault(str(account_id), {})
        fails = int(acc.get("fails") or 0) + 1
        acc["fails"] = fails
        acc["lastFailureAt"] = now
        acc["lastStatus"] = status_code

        applied = None
        if kind == "hard":
            acc["until"] = _next_daily_reset()
            acc["reason"] = "quota"
            # 硬冷却不叠加软退避，签到回血后即刻恢复
            acc["softStreak"] = 0
            applied = "hard"
        elif kind == "soft":
            streak = int(acc.get("softStreak") or 0) + 1
            acc["softStreak"] = streak
            # 指数退避：base × 2^(连续次数-1)，封顶
            dur = min(SOFT_RATE_BASE * (2 ** (streak - 1)), SOFT_RATE_MAX)
            until = now + dur
            # 6004 带重置墙钟时，优先用上游承诺的解封点。
            # 只在它**比退避更晚**时才采纳，避免上游给了一个过去的时间戳
            # 反而缩短冷却（那样会白撞）。
            reset_at = _parse_6004_reset(body_text) if "6004" in (body_text or "") else None
            if reset_at and reset_at > now:
                until = max(until, min(reset_at, now + SOFT_RATE_MAX))
                acc["resetAt"] = reset_at
            acc["until"] = until
            acc["reason"] = "rate_limit"
            applied = "soft"
        elif kind == "not_found":
            acc["until"] = now + NOT_FOUND_COOLDOWN
            acc["reason"] = "not_found"
            applied = "not_found"
        elif kind == "channel":
            # 与 hard 同理：渠道拦截不叠加软退避，到期即恢复。
            # 上游的一次策略判定不该被放大成长期封禁。
            acc["until"] = now + CHANNEL_COOLDOWN
            acc["reason"] = "channel_blocked"
            acc["softStreak"] = 0
            applied = "channel"

        # 熔断：连续失败达阈值，与上面的分类冷却并存（取更晚的解封时间）
        if fails >= BREAKER_THRESHOLD:
            retry = int(acc.get("breakerCount") or 0)
            dur = min(BREAKER_BASE * (2 ** retry), BREAKER_MAX)
            acc["breakerCount"] = retry + 1
            acc["breakerUntil"] = now + dur
            acc["fails"] = 0  # 触发一次后重新计数
            applied = applied or "breaker"

        _save(state)
    return applied


def record_success(account_id: str) -> None:
    """成功请求：清零连续失败与软退避计数（避免偶发抖动被误伤）。

    不清空 ``until`` —— 已经施加的冷却要让它到期，
    否则一个号反复「成功-失败」就能绕过冷却。
    """
    if not account_id:
        return
    with _LOCK:
        state = _load()
        acc = state.get(str(account_id))
        if not acc:
            return
        acc["fails"] = 0
        acc["softStreak"] = 0
        acc["breakerCount"] = 0
        acc["breakerUntil"] = 0
        _save(state)


def is_cooling(account_id: str) -> bool:
    """该账号是否正处于冷却中（未到期则 True）。"""
    if not account_id:
        return False
    with _LOCK:
        state = _load()
    acc = state.get(str(account_id))
    if not acc:
        return False
    now = _now()
    until = float(acc.get("until") or 0)
    breaker = float(acc.get("breakerUntil") or 0)
    cooling = (until > now) or (breaker > now)
    if cooling:
        return True
    # 已到期：顺手清理，避免状态文件无限膨胀
    if until or breaker:
        with _LOCK:
            st = _load()
            a = st.get(str(account_id))
            if a and not ((a.get("until") or 0) > now or (a.get("breakerUntil") or 0) > now):
                a["until"] = 0
                a["breakerUntil"] = 0
                a["reason"] = None
                _save(st)
    return False


def filter_cooling(accounts: List[Dict[str, Any]], id_of) -> List[Dict[str, Any]]:
    """过滤掉冷却中的账号。

    与账号级停用一致的兜底：**全部**都在冷却时回退到原集合 ——
    宁可让上游返回真实错误，也不在网关层编一个「无账号」。
    """
    if not accounts:
        return accounts
    alive = [a for a in accounts if not is_cooling(str(id_of(a) or ""))]
    return alive if alive else accounts


def snapshot() -> Dict[str, Any]:
    """返回当前冷却状态（供面板/排查查看）。"""
    with _LOCK:
        state = _load()
    now = _now()
    out = {}
    for k, v in state.items():
        until = float(v.get("until") or 0)
        breaker = float(v.get("breakerUntil") or 0)
        latest = max(until, breaker)
        out[k] = {
            "cooling": latest > now,
            "reason": v.get("reason"),
            "remainingSec": int(latest - now) if latest > now else 0,
            "fails": int(v.get("fails") or 0),
            "softStreak": int(v.get("softStreak") or 0),
            "breakerCount": int(v.get("breakerCount") or 0),
        }
    return out


def clear(account_id: str) -> None:
    """手动解除某账号的冷却（签到回血、或用户手工恢复时用）。"""
    with _LOCK:
        state = _load()
        acc = state.get(str(account_id))
        if acc:
            acc["until"] = 0
            acc["breakerUntil"] = 0
            acc["reason"] = None
            acc["fails"] = 0
            acc["softStreak"] = 0
            acc["breakerCount"] = 0
            _save(state)
