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
    # IP 白名单：仅当 requireKey=True 时生效。命中名单的客户端免密钥放行。
    # 典型用途：家里/公司的固定出口 IP、内网网关、CI 机器 —— 它们反复调用
    # /v1/*，每次都带密钥既麻烦又容易在轮换时漏改（本项目刚因此踩过 401）。
    "whitelist": [],
}


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def _load_locked() -> None:
    """从磁盘恢复。文件缺失 / 损坏 / 结构不对一律回退到默认值，绝不抛。"""
    _STATE["requireKey"] = False
    _STATE["keys"] = []
    _STATE["whitelist"] = []
    try:
        if not KEYS_FILE.exists():
            return
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        _STATE["requireKey"] = bool(data.get("requireKey"))
        wl = data.get("whitelist")
        if isinstance(wl, list):
            _STATE["whitelist"] = [str(x).strip() for x in wl if str(x).strip()]
        keys = data.get("keys")
        if isinstance(keys, list):
            _STATE["keys"] = [k for k in keys if isinstance(k, dict) and k.get("key")]
    except Exception as e:
        print(f"[api-keys] failed to load keys: {e}", flush=True)
        _STATE["requireKey"] = False
        _STATE["keys"] = []
        _STATE["whitelist"] = []


def _save_locked() -> None:
    """整份重写 + tmp/os.replace 原子替换，进程被 kill 也不会留下半截 JSON。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = KEYS_FILE.with_name(KEYS_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"requireKey": _STATE["requireKey"],
                       "whitelist": _STATE.get("whitelist") or [],
                       "keys": _STATE["keys"]},
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
        # 调用量是**每个密钥**自己的，不是全局的 —— 界面上必须挂在
        # 每个密钥行内，否则会让人误以为是整个网关的调用量。
        "callCount": int(record.get("callCount") or 0),
        # 该密钥自己的 IP 白名单（空 = 不限制来源）。
        # 与全局 whitelist 的区别：全局那份是「免密钥放行」，这份是
        # 「这个密钥只允许从这些 IP 使用」—— 归属到具体密钥上。
        "whitelist": list(record.get("whitelist") or []),
    }


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def get_state() -> Dict[str, Any]:
    with _LOCK:
        return {
            "requireKey": bool(_STATE["requireKey"]),
            "whitelist": list(_STATE.get("whitelist") or []),
        }


# ---------------------------------------------------------------------------
# IP 白名单
# ---------------------------------------------------------------------------
#
# 语义边界（很重要，避免把开关做成后门）：
#   · 只在 requireKey=True 时生效。关掉密钥校验时一切放行，白名单没有意义，
#     前端也据此隐藏这块 UI（配置保留但不可见，重新开启校验即恢复）。
#   · 白名单只能「免去密钥」，不能绕过 loopback 判定之外的其他任何检查。
#   · 支持精确 IP（192.168.31.5）与 CIDR 网段（192.168.31.0/24）。
#     这里**必须**同时支持 CIDR —— 早先给 Hermes 配 NO_PROXY 时踩过
#     「Python 不认 CIDR」的坑，那是另一回事（httpx 的 NO_PROXY 语义），
#     本模块自己解析，不依赖第三方库的环境变量行为。

def _normalize_ip(value: str) -> str:
    """把 IPv4-mapped IPv6（::ffff:192.168.31.5）归一到纯 IPv4 形态。"""
    v = (value or "").strip()
    if v.lower().startswith("::ffff:"):
        v = v[7:]
    return v


def _ip_in_whitelist(host: str, entries: List[str]) -> bool:
    """判断 IP 是否命中白名单。支持精确匹配与 CIDR，非法条目静默跳过。"""
    import ipaddress

    ip = _normalize_ip(host)
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False

    for raw in entries or []:
        item = _normalize_ip(str(raw))
        if not item:
            continue
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif addr == ipaddress.ip_address(item):
                return True
        except ValueError:
            # 用户手输的条目可能不合法。跳过而不是 500 —— 一个笔误不该
            # 让整个鉴权路径挂掉。
            continue
    return False


def _normalize_whitelist(entries: Any) -> List[str]:
    """把白名单入参归一成去重后的字符串列表。

    供**全局白名单**与**单个密钥的白名单**共用 —— 两处解析规则必须一致，
    否则同一个输入框在两处行为不同，很难排查。

    支持两种形态：
      · 字符串：逗号或换行分隔（textarea 里两种写法都常见）
      · 数组：逐项 str 化
    """
    if isinstance(entries, str):
        parts = entries.replace(",", "\n").split("\n")
    elif isinstance(entries, (list, tuple)):
        parts = list(entries)
    else:
        parts = []
    normalized: List[str] = []
    for p in parts:
        s = str(p).strip()
        if s and s not in normalized:
            normalized.append(s)
    return normalized


def set_whitelist(entries: Any) -> Dict[str, Any]:
    """整份替换全局白名单。传进来的可能是逗号/换行分隔的字符串，也可能是数组。"""
    with _LOCK:
        _STATE["whitelist"] = _normalize_whitelist(entries)
        _save_locked()
    return get_config()


def set_key_whitelist(key_id: str, entries: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """给**单个密钥**设置它自己的 IP 白名单。

    为什么要有这个（用户 2026-10-07 反馈「主次关系混乱」）：
    白名单原先只有全局一份，且 UI 上摆在密钥列表**前面**，
    于是看起来像是「整个网关的开关」，而实际上它是要归属到具体密钥的。

    现在每个密钥自带一份：空 = 不限制来源（保持旧行为），
    非空 = 该密钥只允许从这些 IP 使用。
    """
    with _LOCK:
        for record in _STATE["keys"]:
            if str(record.get("id")) != str(key_id):
                continue
            record["whitelist"] = _normalize_whitelist(entries)
            _save_locked()
            return _public(record), None
    return None, "key not found"


def get_config() -> Dict[str, Any]:
    """给 /api-keys/status 用：连接信息面板需要知道当前是否强制密钥。"""
    with _LOCK:
        keys = [_public(k) for k in _STATE["keys"]]
        whitelist = list(_STATE.get("whitelist") or [])
    enabled = [k for k in keys if k["enabled"]]
    return {
        "requireKey": bool(_STATE["requireKey"]),
        "whitelist": whitelist,
        "whitelistCount": len(whitelist),
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
            # 每个密钥独立一份白名单，默认不限制来源
            "whitelist": [],
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
            if "whitelist" in patch:
                # 复用全局白名单那套解析（支持字符串/数组、逗号或换行分隔）
                record["whitelist"] = _normalize_whitelist(patch.get("whitelist"))
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

def authenticate(provided: Optional[str], client_host: Optional[str] = None) -> Dict[str, Any]:
    """校验一个密钥。

    返回 ``{"ok": bool, ...}``，调用方据此决定放行或 401 —— 这里不抛异常，
    否则一次配置错误就会让整个网关不可用。

    ``client_host`` 用于校验**该密钥自己的**来源 IP 白名单：
    密钥若配了 whitelist，则只允许名单内的来源使用；未配（空）则不限制。
    留空该参数 = 跳过这项检查（保持旧行为，便于单测与内部调用）。
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
            # 该密钥自己的来源限制：配了才校验，没配就不限制。
            # 与全局白名单（免密钥）是**两回事**：那份是「不带密钥也放行」，
            # 这份是「这个密钥只许这些 IP 用」。
            kwl = record.get("whitelist") or []
            if kwl and client_host:
                if not _ip_in_whitelist(client_host, kwl):
                    return {
                        "ok": False,
                        "mode": "ip_denied",
                        "requireKey": True,
                        "keyId": record.get("id"),
                        "keyName": record.get("name"),
                        "detail": "该密钥不允许从当前来源 IP 使用（已配置来源白名单）。",
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
