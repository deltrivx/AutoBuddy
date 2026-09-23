"""API 密钥管理。

网关对外提供的 OpenAI 兼容接口是可选的访问控制：

- ``requireKey = false``（默认）：任何客户端都能调用，行为与升级前完全一致，
  不配置密钥的用户不会被打断。
- ``requireKey = true``：``/v1/*`` 必须携带有效密钥，否则 401。

设计取舍：

1. **密钥明文存储**。这是自托管的 NAS 工具，密钥要在 WebUI 里随时可复制、可核对；
   若只存哈希，用户丢了就只能在网盘里翻聊天记录。文件权限收紧到 ``0600``。
2. **容器内 loopback 免校验**。WebUI 代理（18090）与网关（18091）同容器，
   代理读 ``/v1/models`` 用于账号卡片的模型清单展示，不能因为开了密钥校验就把 UI 弄坏。
   容器外的请求经 docker NAT 进来，源地址不会是 127.0.0.1，因此放行 loopback 是安全的。
3. **校验失败一律不抛**。配置损坏时回退到「未启用密钥」而不是把网关打死 ——
   与 ``selection_logs.json`` 的容错策略一致。
"""

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
KEYS_FILE = DATA_DIR / "api_keys.json"

KEY_PREFIX = "sk-ab-"
KEY_BODY_LEN = 32

DEFAULT_KEY_NAME = "默认密钥"

_LOCK = threading.RLock()

_STATE: Dict[str, Any] = {
    "requireKey": False,
    "keys": [],
}


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def _load_locked() -> None:
    """从磁盘恢复。文件缺失 / 损坏 / 结构不对一律回退到默认值，绝不抛。"""
    _STATE["requireKey"] = False
    _STATE["keys"] = []
    try:
        if not KEYS_FILE.exists():
            return
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        _STATE["requireKey"] = bool(data.get("requireKey"))
        keys = data.get("keys")
        if isinstance(keys, list):
            _STATE["keys"] = [k for k in keys if isinstance(k, dict) and k.get("key")]
    except Exception as e:
        print(f"[api-keys] failed to load keys: {e}", flush=True)
        _STATE["requireKey"] = False
        _STATE["keys"] = []


def _save_locked() -> None:
    """整份重写 + tmp/os.replace 原子替换，进程被 kill 也不会留下半截 JSON。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = KEYS_FILE.with_name(KEYS_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"requireKey": _STATE["requireKey"], "keys": _STATE["keys"]},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, KEYS_FILE)
        try:
            os.chmod(KEYS_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[api-keys] failed to persist keys: {e}", flush=True)


_load_locked()


# ---------------------------------------------------------------------------
# 展示辅助
# ---------------------------------------------------------------------------

def mask_key(value: str) -> str:
    """只露头尾，中间用圆点，避免截图/录屏时整串泄露。"""
    value = value or ""
    if len(value) <= len(KEY_PREFIX) + 8:
        return value
    head = value[:len(KEY_PREFIX) + 4]
    tail = value[-4:]
    return f"{head}{'•' * 8}{tail}"


def _public(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": record.get("id"),
        "name": record.get("name") or DEFAULT_KEY_NAME,
        "key": record.get("key"),
        "maskedKey": mask_key(record.get("key") or ""),
        "enabled": bool(record.get("enabled", True)),
        "createdAt": record.get("createdAt"),
        "lastUsedAt": record.get("lastUsedAt"),
        "callCount": int(record.get("callCount") or 0),
    }


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def get_state() -> Dict[str, Any]:
    with _LOCK:
        return {"requireKey": bool(_STATE["requireKey"])}


def get_config() -> Dict[str, Any]:
    """给 /api-keys/status 用：连接信息面板需要知道当前是否强制密钥。"""
    with _LOCK:
        keys = [_public(k) for k in _STATE["keys"]]
    enabled = [k for k in keys if k["enabled"]]
    return {
        "requireKey": bool(_STATE["requireKey"]),
        "keys": keys,
        "stats": {
            "total": len(keys),
            "enabled": len(enabled),
            "calls": sum(k["callCount"] for k in keys),
            "lastUsedAt": max([k["lastUsedAt"] or 0 for k in keys] or [0]) or None,
        },
    }


def set_require_key(value: Any) -> Dict[str, Any]:
    with _LOCK:
        _STATE["requireKey"] = bool(value)
        _save_locked()
    return get_config()


# ---------------------------------------------------------------------------
# 密钥增删改
# ---------------------------------------------------------------------------

def _new_key_value() -> str:
    return KEY_PREFIX + secrets.token_hex(KEY_BODY_LEN // 2)


def create_key(name: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """新建密钥。返回 (记录, 错误)。"""
    with _LOCK:
        record = {
            "id": "k" + secrets.token_hex(4),
            "name": (name or "").strip() or _auto_name_locked(),
            "key": _new_key_value(),
            "enabled": True,
            "createdAt": int(time.time() * 1000),
            "lastUsedAt": None,
            "callCount": 0,
        }
        _STATE["keys"].append(record)
        _save_locked()
    return _public(record), None


def _auto_name_locked() -> str:
    """没起名字就给一个不撞车的默认名。"""
    existing = {str(k.get("name")) for k in _STATE["keys"]}
    if DEFAULT_KEY_NAME not in existing:
        return DEFAULT_KEY_NAME
    index = 2
    while f"{DEFAULT_KEY_NAME} {index}" in existing:
        index += 1
    return f"{DEFAULT_KEY_NAME} {index}"


def update_key(key_id: str, patch: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    with _LOCK:
        for record in _STATE["keys"]:
            if str(record.get("id")) != str(key_id):
                continue
            if "name" in patch:
                name = str(patch.get("name") or "").strip()
                if name:
                    record["name"] = name
            if "enabled" in patch:
                record["enabled"] = bool(patch.get("enabled"))
            _save_locked()
            return _public(record), None
    return None, "key not found"


def delete_key(key_id: str) -> bool:
    with _LOCK:
        before = len(_STATE["keys"])
        _STATE["keys"] = [k for k in _STATE["keys"] if str(k.get("id")) != str(key_id)]
        changed = len(_STATE["keys"]) != before
        if changed:
            _save_locked()
    return changed


def delete_all_keys() -> int:
    with _LOCK:
        count = len(_STATE["keys"])
        _STATE["keys"] = []
        _save_locked()
    return count


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

def authenticate(provided: Optional[str]) -> Dict[str, Any]:
    """校验一个密钥。

    返回 ``{"ok": bool, ...}``，调用方据此决定放行或 401 —— 这里不抛异常，
    否则一次配置错误就会让整个网关不可用。
    """
    with _LOCK:
        require = bool(_STATE["requireKey"])
        keys = list(_STATE["keys"])

    if not require:
        return {"ok": True, "mode": "open", "requireKey": False}

    token = (provided or "").strip()
    if not token:
        return {
            "ok": False,
            "mode": "missing",
            "requireKey": True,
            "detail": "缺少 API 密钥：请在请求头携带 Authorization: Bearer <key> 或 x-api-key: <key>。",
        }

    for record in keys:
        if secrets.compare_digest(str(record.get("key") or ""), token):
            if not record.get("enabled", True):
                return {
                    "ok": False,
                    "mode": "disabled",
                    "requireKey": True,
                    "detail": "该 API 密钥已被停用。",
                }
            return {
                "ok": True,
                "mode": "key",
                "requireKey": True,
                "keyId": record.get("id"),
                "keyName": record.get("name"),
            }

    return {
        "ok": False,
        "mode": "invalid",
        "requireKey": True,
        "detail": "API 密钥无效。",
    }


_LAST_SAVE_AT = 0.0
_SAVE_DEBOUNCE_SEC = 5.0


def mark_used(key_id: Optional[str]) -> None:
    """记录一次成功调用。

    高频路径上不做每次落盘：内存先更新，磁盘最多每 ``_SAVE_DEBOUNCE_SEC`` 秒写一次。
    容器被强杀最多丢最后几秒的计数，但不值得让每个 API 请求都去碰磁盘。
    统计失败一律吞掉 —— 绝不能影响正常请求。
    """
    global _LAST_SAVE_AT
    if not key_id:
        return
    try:
        now = time.time()
        with _LOCK:
            for record in _STATE["keys"]:
                if str(record.get("id")) == str(key_id):
                    record["lastUsedAt"] = int(now * 1000)
                    record["callCount"] = int(record.get("callCount") or 0) + 1
                    break
            if now - _LAST_SAVE_AT >= _SAVE_DEBOUNCE_SEC:
                _LAST_SAVE_AT = now
                _save_locked()
    except Exception as e:
        print(f"[api-keys] failed to mark usage: {e}", flush=True)
