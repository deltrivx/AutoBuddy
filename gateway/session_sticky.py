"""会话粘性路由（同一会话尽量绑定同一账号）。

参考 workbuddy2api-panel（2009★）：

> 🧲 会话粘性：同一会话（`conversation_id`）尽量绑定同一账号，TTL 滚动续期，
> 失败自动解绑，可镜像 Redis 防重启丢失
> 粘性会话内容回退：客户端不发 `conversation_id` 时，用 `system + 首条 user`
> 哈希派生会话键（`d-` 前缀），通用 OpenAI 客户端也能享受粘性

为什么需要：AutoBuddy 此前是**每请求独立选号**（请求级并发分摊），
多轮对话会在账号之间跳来跳去。上游对同一会话的上下文连续性没有保证，
跳号表现为「多轮对话突然忘了前面说了什么」。

设计取舍：

- **单实例用内存即可**：容器重启后客户端的多轮上下文本来也就断了，
  panel 用 Redis 是为了多实例共享；我们单实例，落盘反而是无谓的复杂度。
- **粘性是软偏好**：粘性账号若已冷却、被停用、或不支持该模型，
  自动落回普通选号 —— 绝不能因为粘性而把一个坏账号硬塞给请求。
- **失败即解绑**：撞上 429/402 立刻解绑，下次换号。
"""

import hashlib
import os
import threading
import time
from typing import Any, Dict, Optional

ENABLED = os.getenv("AB_STICKY_ENABLED", "1").strip().lower() not in ("0", "false", "no")
STICKY_TTL = int(os.getenv("AB_STICKY_TTL", "1800") or 1800)  # 默认 30m
MAX_BINDINGS = int(os.getenv("AB_STICKY_MAX", "2000") or 2000)

_LOCK = threading.Lock()
# session_key -> {"accountId": str, "expireAt": float}
_BINDINGS: Dict[str, Dict[str, Any]] = {}


def _content_text(content: Any) -> str:
    """把 content 归一成文本（兼容数组形态的多模态 content）。"""
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text") or ""))
            else:
                parts.append(str(c))
        return " ".join(parts)
    return str(content or "")


def derive_session_key(body: Optional[Dict[str, Any]],
                       headers: Optional[Dict[str, str]] = None) -> Optional[str]:
    """从请求派生会话键；无法派生则返回 None（不启用粘性）。

    优先级：
    1. 显式 `conversation_id`（body 或 `x-conversation-id` 头）→ `c-` 前缀；
    2. 用 `system + 首条 user` 内容哈希 → `d-` 前缀（panel 的回退做法）。
    """
    if not isinstance(body, dict):
        return None
    cid = body.get("conversation_id")
    if not cid and headers:
        cid = headers.get("x-conversation-id") or headers.get("X-Conversation-Id")
    if cid:
        return "c-" + str(cid)

    msgs = body.get("messages") or []
    sys_txt = ""
    user_txt = ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _content_text(m.get("content"))
        if role == "system" and not sys_txt:
            sys_txt = text[:300]
        elif role == "user" and not user_txt:
            user_txt = text[:300]
        if sys_txt and user_txt:
            break
    if not (sys_txt or user_txt):
        return None
    raw = sys_txt + "|" + user_txt
    return "d-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def get_bound(session_key: Optional[str]) -> Optional[str]:
    """取会话绑定的账号 id；命中则滚动续期 TTL。"""
    if not ENABLED or not session_key:
        return None
    now = time.time()
    with _LOCK:
        b = _BINDINGS.get(session_key)
        if not b:
            return None
        if float(b.get("expireAt") or 0) <= now:
            _BINDINGS.pop(session_key, None)
            return None
        # 滚动续期：会话还在持续，就别让绑定过期
        b["expireAt"] = now + STICKY_TTL
        return b.get("accountId")


def bind(session_key: Optional[str], account_id: Optional[str]) -> None:
    """绑定会话到账号。"""
    if not ENABLED or not session_key or not account_id:
        return
    with _LOCK:
        _BINDINGS[session_key] = {
            "accountId": str(account_id),
            "expireAt": time.time() + STICKY_TTL,
        }
        _gc_locked()


def unbind(session_key: Optional[str]) -> None:
    """解除绑定（失败时调用，下次请求重新选号）。"""
    if not session_key:
        return
    with _LOCK:
        _BINDINGS.pop(session_key, None)


def _gc_locked() -> None:
    """清理过期绑定并控制容量（调用方需持锁）。"""
    now = time.time()
    for k in [k for k, v in _BINDINGS.items()
              if float(v.get("expireAt") or 0) <= now]:
        _BINDINGS.pop(k, None)
    if len(_BINDINGS) > MAX_BINDINGS:
        # 丢最早过期的
        over = len(_BINDINGS) - MAX_BINDINGS
        for k in sorted(_BINDINGS,
                        key=lambda kk: float(_BINDINGS[kk].get("expireAt") or 0))[:over]:
            _BINDINGS.pop(k, None)


def snapshot() -> Dict[str, Any]:
    """当前绑定情况（排查用）。"""
    with _LOCK:
        now = time.time()
        return {
            k: {"accountId": v.get("accountId"),
                "remainingSec": int(float(v.get("expireAt") or 0) - now)}
            for k, v in _BINDINGS.items()
            if float(v.get("expireAt") or 0) > now
        }
