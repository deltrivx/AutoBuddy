"""账号风控标记（上游明确判定「这个账号被拦了」的独立状态）。

## 为什么必须有这个模块

2026-10-17 的事故根因：**本地把「被上游风控标记」和「本地停用」混为一谈**。

当时 17 个账号在一轮巡检里被判定为凭据失效，`model_health.py` 把
`auth_failed` / `restricted` / `quota_exhausted` 三类一起送进
`account_policy.set_disabled(..., SOURCE_AUTO)`，于是一次上游抖动变成了
17 个账号集体「本地停用」。用户看到的是一片「已停用（巡检）」，
分不清哪个是真过期、哪个只是被风控、哪个是没钱了。

而 ``account_policy.json`` 的结构是**扁平**的 ``{account_id: source}``
（source 只有 manual / auto 两种），它在物理上就无法承载
「被上游标记」这个维度 —— 复用它就是再混一次。

因此：风控标记独立成档，与停用表**互不写入**。

## 三条设计红线（写代码前先读）

1. **只记录，不处置。** 本模块没有任何停用账号的代码路径，
   也不 import ``account_policy``。账号要不要停只由人决定。
2. **标记 ≠ 停用。** 一个账号可以「被风控标记但仍在跑」
   （`autoDisableAccounts` 已关，标记不会阻止它参与轮询），
   也可以「本地停用但从未被标记」。两者是正交的两个维度。
3. **不动账号数据文件。** 本模块只读写自己的 ``account_risk_flags.json``，
   ``accounts.json`` 两份都不碰。

## 数据形状

```json
{
  "version": 1,
  "flags": {
    "<account_id>": {
      "code": 11140,
      "reason": "账号存在异常行为",
      "firstSeenAt": 1792250222634,
      "lastSeenAt": 1792250222634,
      "hits": 3,
      "source": "probe"
    }
  }
}
```

``source`` 记录是谁发现的：``probe`` = 可用性巡检的账号级探测，
``forward`` = API 转发时上游返回的 403。两者都算确诊，
只是发现路径不同。

``hits`` 是历史累计触发次数（从不清零，除非账号被删）——
界面要展示「历史触发次数」，需要这个数一直累加。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
STATE_FILE = DATA_DIR / "account_risk_flags.json"

# 会被认定为「账号被上游风控/策略拦截」的错误码。
# 与 model_health._CODE_SEMANTICS 里语义为 restricted 的码保持一致 ——
# 那张表是唯一事实来源，这里只取其中一个子集（要推送给人的那部分）。
# 为什么不直接 import model_health：它依赖 httpx 等重型依赖，
# 而本模块要被转发路径（每次上游错误都会走）引入，不能拖慢主流程。
RISK_CODES = (11140,)

# 推送冷却：同一账号两次推送的最小间隔（秒）。
# 与 v1.0.13 的 account_risk_alert（1h）保持一致口径 ——
# 风控标记是持续状态，不是每次失败都该叫一次人。
ALERT_COOLDOWN = int(os.getenv("AB_RISK_FLAG_ALERT_COOLDOWN", "3600") or 3600)

_LOCK = threading.Lock()

# 预警回调（由 gateway/main.py 注入）。签名：
#   callback(account_id, flag)  flag 为上面的 dict 形状
_ALERT_CALLBACK = None


def _load() -> Dict[str, Any]:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("flags"), dict):
                return data
    except Exception:
        pass
    return {"version": 1, "flags": {}}


def _save(state: Dict[str, Any]) -> None:
    """原子落盘。失败不影响主流程 —— 标记是附属信息，不是账号凭据。"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(STATE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _reason_of(code: int, snippet: str = "") -> str:
    """把上游错误码翻成一句人话。上游给了 msg 就优先用它的原文。"""
    for probe in (RISK_CODES,):
        if code in probe:
            break
    else:
        return "账号被上游拦截"
    # 上游 msg 形如「账号存在异常行为」，直接拿来用比我们翻译的更准。
    if snippet:
        try:
            payload = json.loads(snippet)
            msg = (payload.get("data") or {}).get("msg") if isinstance(payload, dict) else None
            if not msg:
                msg = payload.get("msg") if isinstance(payload, dict) else None
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:80]
        except Exception:
            pass
    return "账号存在异常行为"


def record(account_id: str, code: int, snippet: str = "",
           source: str = "forward") -> Optional[Dict[str, Any]]:
    """记录一次风控标记；返回该账号当前的标记（首次或更新后），否则 None。

    只在上游错误码命中 ``RISK_CODES`` 时才写。其余错误码（401/429/402…）
    一律不碰 —— 那些有各自的治理路径（cooldown / model_health），
    塞进来只会让「风控标记」失去确诊的含义。
    """
    # code 一律按数字处理：非数字（None、空串、上游文案）直接不记。
    # 不 try 住 int() 会让一次脏数据把记账流程抛炸 —— 而记账挂在转发
    # 主路径上，抛出去就是 5xx 给客户端。
    try:
        code_int = int(code or 0)
    except (TypeError, ValueError):
        return None
    if not account_id or code_int not in RISK_CODES:
        return None
    now_ms = int(time.time() * 1000)
    fire = False
    with _LOCK:
        state = _load()
        flags = state.setdefault("flags", {})
        acc = flags.get(str(account_id))
        if acc is None:
            acc = {
                "code": code_int,
                "reason": _reason_of(code_int, snippet),
                "firstSeenAt": now_ms,
                "lastSeenAt": now_ms,
                "hits": 1,
                "source": str(source or "forward"),
                # 首次就要写下推送时刻：漏写它，第二次进来时
                # int(None or 0) * 1000 == 0，冷却判定必然通过 → 立刻再推一次。
                # （单位是**秒**，与下面的比对保持一致。）
                "alertedAt": now_ms // 1000,
            }
            flags[str(account_id)] = acc
            fire = True
        else:
            acc["code"] = code_int
            # reason 可能随上游文案更新，取最新的一份
            acc["reason"] = _reason_of(code_int, snippet)
            acc["lastSeenAt"] = now_ms
            acc["hits"] = int(acc.get("hits") or 0) + 1
            # source 以最近一次发现路径为准（probe 比 forward 更权威）
            if str(source or "") == "probe":
                acc["source"] = "probe"
            # 冷却期内不重复叫人。alertedAt 记的是**秒**，这里统一换算出毫秒再比，
            # 避免「一边秒一边毫秒」的单位错位（那会让冷却永远不生效或永远生效）。
            _alerted_at_ms = int(acc.get("alertedAt") or 0) * 1000
            if now_ms - _alerted_at_ms >= ALERT_COOLDOWN * 1000:
                fire = True
            acc["alertedAt"] = now_ms // 1000
        _save(state)
        snapshot = dict(acc)

    if fire:
        _fire_alert(str(account_id), snapshot)
    return snapshot


def _fire_alert(account_id: str, flag: Dict[str, Any]) -> None:
    """触发推送。通知是附属能力，任何异常都不影响记录主流程。"""
    cb = _ALERT_CALLBACK
    if cb is None:
        return
    try:
        cb(account_id, flag)
    except Exception:
        pass


def set_alert_callback(cb) -> None:
    """注册风控标记推送回调（gateway/main.py 用它接 notify）。"""
    global _ALERT_CALLBACK
    _ALERT_CALLBACK = cb


def get(account_id: str) -> Optional[Dict[str, Any]]:
    """取某账号的风控标记（没有则 None）。"""
    if not account_id:
        return None
    with _LOCK:
        state = _load()
    acc = (state.get("flags") or {}).get(str(account_id))
    return dict(acc) if isinstance(acc, dict) else None


def all_flags() -> Dict[str, Dict[str, Any]]:
    """全部标记（账号 id → 标记 dict）。界面据此给卡片打角标。"""
    with _LOCK:
        state = _load()
    out = {}
    for k, v in (state.get("flags") or {}).items():
        if isinstance(v, dict):
            out[str(k)] = dict(v)
    return out


def flagged_ids() -> set:
    """被标记的账号 id 集合。"""
    return set(all_flags().keys())


def clear(account_id: str) -> None:
    """解除某账号的风控标记。

    只应由**人工**触发（账号管理界面的「解除标记」按钮）——
    本模块没有任何自动清除逻辑：上游没说它好了，我们就不该擅自说好了。
    自动解除正是「标记」失去意义的方式。
    """
    if not account_id:
        return
    with _LOCK:
        state = _load()
        flags = state.get("flags") or {}
        if str(account_id) in flags:
            flags.pop(str(account_id), None)
            _save(state)


def remove_account(account_id: str) -> None:
    """账号被删除时清理其标记（与 model_policy / account_policy 的同名清理对齐）。

    不清会留下孤立条目，将来 ID 被复用时旧标记会意外生效。
    """
    clear(account_id)
