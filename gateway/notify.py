"""通知管理：多渠道通知框架（参照 MoviePilot 的通知配置模型）。

MoviePilot 的做法（实测其 app/modules/wechat/wechat.py）：
  - 企微渠道三要素：WECHAT_CORPID / WECHAT_APP_SECRET / WECHAT_APP_ID(AgentId)
  - 流程：gettoken（corpid+corpsecret）→ message/send（agentid + content）
  - API 域默认 https://qyapi.weixin.qq.com，可用代理覆盖

本模块先做**框架 + 企业微信第一个渠道**（用户 2026-10-13 要求）：
  - 渠道注册表 CHANNELS：后续加 Telegram/钉钉/Bark 等时逐个注册即可，
    前端按注册表自动渲染配置项，后端 send() 按渠道名分发；
  - 配置持久化 DATA_DIR/notify_config.json（与 model_policy.json 同级同风格）；
  - 凭据只存服务端文件；secret 不回传前端（列表接口脱敏，只回 has_secret）；
  - 测试接口 POST /api/notify/test：保存当前表单 → 真发一条测试消息 → 返回上游结果。

安全红线：secret 不落日志、不进 GET 响应正文；发送失败把上游 msg 带回
（抹掉 access_token），方便用户排查 corpid/secret/agentid 填错。
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
CONFIG_FILE = DATA_DIR / "notify_config.json"

_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"mtime": 0.0, "data": None}

# ---------------------------------------------------------------------------
# 渠道注册表：每个渠道声明自己的配置字段（前端据此渲染表单）。
# secret=True 的字段：GET 时只回 has_secret 布尔，不回值。
# ---------------------------------------------------------------------------
CHANNELS: List[Dict[str, Any]] = [
    {
        "key": "wecom",
        "name": "企业微信",
        "desc": "企业微信群机器人 / 应用消息。需要企业 ID（CorPID）、应用 Secret、"
                "应用 AgentId，三者都在企业微信管理后台的「应用管理」页。",
        "fields": [
            {"key": "corpid", "label": "企业 ID（CorPID）", "secret": False,
             "placeholder": "ww 开头的企业 ID"},
            {"key": "secret", "label": "应用 Secret", "secret": True,
             "placeholder": "应用的凭证密钥"},
            {"key": "agentid", "label": "AgentId（应用 ID）", "secret": False,
             "placeholder": "纯数字，如 1000002"},
            {"key": "touser", "label": "接收人", "secret": False,
             "placeholder": "成员账号，多个人用 | 分隔；@all 发给全员"},
        ],
    },
    # 后续渠道（Telegram / 钉钉 / Bark…）往这里追加注册即可，
    # 前端表单与后端分发都会自动带上。
]

_DEFAULT_CFG: Dict[str, Any] = {
    "enabled": False,
    "channels": {c["key"]: {} for c in CHANNELS},
}


def _load() -> Dict[str, Any]:
    """读配置（mtime 缓存）。读不到回默认骨架，绝不让设置页打不开。"""
    try:
        if not CONFIG_FILE.exists():
            return json.loads(json.dumps(_DEFAULT_CFG))
        mtime = CONFIG_FILE.stat().st_mtime
        with _LOCK:
            if _CACHE["mtime"] == mtime and _CACHE["data"] is not None:
                return _CACHE["data"]
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = json.loads(json.dumps(_DEFAULT_CFG))
        data.setdefault("enabled", False)
        data.setdefault("channels", {})
        for c in CHANNELS:
            data["channels"].setdefault(c["key"], {})
        with _LOCK:
            _CACHE["mtime"] = mtime
            _CACHE["data"] = data
        return data
    except Exception:
        return json.loads(json.dumps(_DEFAULT_CFG))


def _save(data: Dict[str, Any]) -> None:
    """原子落盘（先写临时文件再 replace），并同步缓存。"""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
    with _LOCK:
        _CACHE["mtime"] = CONFIG_FILE.stat().st_mtime
        _CACHE["data"] = data


def _sanitize_token(text: str) -> str:
    """抹掉上游返回里的 access_token，防泄漏进日志/前端。"""
    return re.sub(r"access_token=[A-Za-z0-9_\-]+", "access_token=***", text or "")


# ---------------------------------------------------------------------------
# API 视图：GET 用的脱敏结构
# ---------------------------------------------------------------------------
def public_view() -> Dict[str, Any]:
    cfg = _load()
    chans: Dict[str, Any] = {}
    for c in CHANNELS:
        raw = cfg["channels"].get(c["key"]) or {}
        view: Dict[str, Any] = {}
        for f in c["fields"]:
            v = str(raw.get(f["key"]) or "").strip()
            if f["secret"]:
                view[f["key"]] = {"has_value": bool(v)}
            else:
                view[f["key"]] = v
        # 配齐三要素才算「可测试/可用」（touser 可选，缺省 @all）
        ready = bool(view.get("corpid")
                     and (view.get("secret") or {}).get("has_value")
                     and view.get("agentid"))
        view["_ready"] = ready
        chans[c["key"]] = view
    return {"enabled": bool(cfg.get("enabled")), "channels": chans,
            "registry": CHANNELS}


def update_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """保存渠道配置。

    secret 字段：payload 传空字符串 = 清除；不传该键 = 保留原值
    （前端「已配置，留空保持不变」的语义）。其余字段直接覆盖。
    """
    cfg = _load()
    body = payload or {}
    if "enabled" in body:
        cfg["enabled"] = bool(body.get("enabled"))
    incoming = body.get("channels")
    if isinstance(incoming, dict):
        for c in CHANNELS:
            newv = incoming.get(c["key"])
            if not isinstance(newv, dict):
                continue
            slot = cfg["channels"].setdefault(c["key"], {})
            for f in c["fields"]:
                if f["key"] not in newv:
                    continue
                v = newv.get(f["key"])
                if f["secret"]:
                    if v is None:
                        continue          # 未提交 → 保持
                    slot[f["key"]] = str(v or "").strip()
                else:
                    slot[f["key"]] = str(v or "").strip()
    _save(cfg)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 发送：按渠道分发。这里先实现企业微信（MoviePilot 同款流程）。
# ---------------------------------------------------------------------------
def _send_wecom(ch_cfg: Dict[str, Any], text: str) -> Dict[str, Any]:
    corpid = str(ch_cfg.get("corpid") or "").strip()
    secret = str(ch_cfg.get("secret") or "").strip()
    agentid = str(ch_cfg.get("agentid") or "").strip()
    touser = str(ch_cfg.get("touser") or "").strip() or "@all"
    if not (corpid and secret and agentid):
        return {"ok": False, "error": "企业微信配置不完整（CorPID/Secret/AgentId）"}
    try:
        agentid_num = int(agentid)
    except ValueError:
        return {"ok": False, "error": "AgentId 必须是纯数字（企微后台应用页的 AgentId）"}
    try:
        import httpx
        base = "https://qyapi.weixin.qq.com"
        with httpx.Client(timeout=20.0, verify=False) as client:
            tr = client.get(base + "/cgi-bin/gettoken",
                            params={"corpid": corpid, "corpsecret": secret})
            td = tr.json() if tr.status_code == 200 else {}
            token = (td or {}).get("access_token")
            if not token:
                return {"ok": False,
                        "error": "获取 access_token 失败：errcode=%s %s" % (
                            (td or {}).get("errcode"), _sanitize_token(str((td or {}).get("errmsg") or ""))[:120])}
            mr = client.post(base + "/cgi-bin/message/send",
                             params={"access_token": token},
                             json={"touser": touser, "msgtype": "text",
                                   "agentid": agentid_num,
                                   "text": {"content": text}})
            md = mr.json() if mr.status_code == 200 else {}
            if (md or {}).get("errcode") == 0:
                return {"ok": True}
            return {"ok": False,
                    "error": "发送失败：errcode=%s %s" % (
                        (md or {}).get("errcode"), _sanitize_token(str((md or {}).get("errmsg") or ""))[:160])}
    except Exception as e:
        return {"ok": False, "error": "请求异常：%s" % str(e)[:120]}


_SENDERS = {"wecom": _send_wecom}


def send(text: str, channel: Optional[str] = None) -> Dict[str, Any]:
    """发送一条通知。channel=None 时发给所有已配置就绪的渠道。"""
    cfg = _load()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "通知总开关未开启"}
    targets = [channel] if channel else [c["key"] for c in CHANNELS]
    results: Dict[str, Any] = {}
    any_ok = False
    for key in targets:
        if key not in _SENDERS:
            continue
        r = _SENDERS[key](cfg["channels"].get(key) or {}, text)
        results[key] = r
        any_ok = any_ok or bool(r.get("ok"))
    return {"ok": any_ok, "results": results}


def send_test(channel: str, text: str) -> Dict[str, Any]:
    """测试发送：不要求总开关（用户填完表单想立刻验证连通性）。"""
    if channel not in _SENDERS:
        return {"ok": False, "error": "未知渠道：%s" % channel}
    cfg = _load()
    return _SENDERS[channel](cfg["channels"].get(channel) or {},
                             text or "AutoBuddy 通知测试 · %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
