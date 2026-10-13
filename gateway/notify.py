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
            # 回调配置（接收消息服务器）：企微后台「接收消息」页的三个值。
            # 配好后不仅能收消息（交互），还解锁「企业可信IP」的前置条件。
            {"key": "cb_token", "label": "回调 Token", "secret": False,
             "placeholder": "接收消息服务器的 Token（企微后台生成/自定义）"},
            {"key": "cb_aes_key", "label": "回调 EncodingAESKey", "secret": False,
             "placeholder": "43 位加密密钥（企微后台生成）"},
            {"key": "cb_url", "label": "回调 URL", "secret": False,
             "placeholder": "公网可达地址，如 http://<VPS>:10083/api/notify/wecom/callback/<随机段>"},
        ],
    },
    {
        "key": "telegram",
        "name": "Telegram",
        "desc": "Telegram 机器人。Bot Token 由 @BotFather 创建机器人时给出；"
                "Chat ID 可从 @userinfobot 或机器人收到的消息里取。",
        "fields": [
            {"key": "bot_token", "label": "Bot Token", "secret": True,
             "placeholder": "@BotFather 给出的 Token"},
            {"key": "chat_id", "label": "Chat ID", "secret": False,
             "placeholder": "接收消息的会话 ID，如 8646289271"},
            # 渠道级代理：只用于本通道的通知与交互，不写进全局 PROXY、
            # 也不影响网关出网（用户 2026-10-13 要求），与 MoviePilot 的
            # 全局 PROXY 不同——那里是全局一份，这里是每渠道独立。
            {"key": "proxy", "label": "代理（仅本通道）", "secret": False,
             "placeholder": "留空=直连；如 http://192.168.31.10:7890"},
        ],
    },
    # 后续渠道（钉钉 / Bark…）往这里追加注册即可，
    # 前端表单与后端分发都会自动带上。
]

# 消息场景（MoviePilot 的 notificationTypes 对应物）：
# 渠道可按场景订阅，空 = 全部场景（默认）。
#
# 2026-10-13 按用户需求补齐 5 类场景（每类都有独立标题/说明/启用开关）：
#   daily_report   —— 每日凌晨 0 点的余额与调用日报（今日消耗改用「昨日消耗」）
#   checkin_done   —— 签到成功后推送签到积分/剩余积分/总积分
#   task_done      —— 每日任务成功后推送任务积分与当前总积分
#   health_done    —— 巡检结束后推送结果与当前可用模型
#   model_change   —— 上游模型目录变化时推送变化与当前可用模型
SCENES: List[Dict[str, str]] = [
    {"key": "daily_report", "name": "每日凌晨报告",
     "desc": "每天 00:05 推送昨日消耗、可用模型、通知链路状态、各 API 当天与累计调用次数"},
    {"key": "checkin_done", "name": "签到成功通知",
     "desc": "签到成功后推送本次签到积分、剩余积分与总积分"},
    {"key": "task_done", "name": "每日任务完成",
     "desc": "每日任务执行成功后推送任务积分与当前总积分"},
    {"key": "health_done", "name": "巡检结果通知",
     "desc": "模型可用性巡检结束后推送结果摘要与当前可用模型"},
    {"key": "model_change", "name": "模型目录变化",
     "desc": "上游模型目录新增或下线时推送变化明细与当前可用模型"},
    # 保留的通用场景
    {"key": "account_low", "name": "账号余额不足",
     "desc": "账号余额低于阈值时提醒"},
    {"key": "system", "name": "系统异常",
     "desc": "网关或任务执行出现异常时提醒"},
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
        if c["key"] == "telegram":
            ready = bool((view.get("bot_token") or {}).get("has_value")
                         and view.get("chat_id"))
        view["_ready"] = ready
        # 每渠道独立启用（MoviePilot 的 NotificationConf.enabled 语义）
        raw_ch = cfg["channels"].get(c["key"]) or {}
        view["_enabled"] = bool(raw_ch.get("_enabled", True))
        # 消息场景开关（MoviePilot 的 switchs 语义；空 = 全部场景）
        view["_scenes"] = raw_ch.get("_scenes") or []
        chans[c["key"]] = view
    return {"enabled": bool(cfg.get("enabled")), "channels": chans,
            "registry": CHANNELS, "scenes": SCENES}


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
            # 每渠道启用与场景（MoviePilot 的 enabled / switchs 语义）
            if isinstance(newv.get("_enabled"), bool):
                slot["_enabled"] = newv["_enabled"]
            if isinstance(newv.get("_scenes"), list):
                slot["_scenes"] = [str(x) for x in newv["_scenes"]]
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


def _send_telegram(ch_cfg: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Telegram Bot 发送（渠道级代理，不影响全局/网关）。

    与 MoviePilot 的差异（如实说明）：它用**全局** PROXY 一份；
    本实现按用户要求做成**每渠道独立**代理 —— 只作用于 Telegram 通道的
    通知与交互，不写进全局设置、不改网关出网行为。
    """
    token = str(ch_cfg.get("bot_token") or "").strip()
    chat_id = str(ch_cfg.get("chat_id") or "").strip()
    if not (token and chat_id):
        return {"ok": False, "error": "Telegram 配置不完整（Bot Token / Chat ID）"}
    proxy = str(ch_cfg.get("proxy") or "").strip()
    try:
        import httpx
        url = "https://api.telegram.org/bot%s/sendMessage" % token
        kwargs = {"timeout": 20.0, "verify": False}
        # 仅本通道代理：直接传给该请求，不动任何全局/环境变量
        if proxy:
            kwargs["proxy"] = proxy
        with httpx.Client(**kwargs) as client:
            r = client.post(url, json={"chat_id": chat_id, "text": text})
            d = r.json() if r.status_code == 200 else {}
        if (d or {}).get("ok") is True:
            return {"ok": True}
        return {"ok": False, "error": "发送失败：%s" % _sanitize_token(
            str((d or {}).get("description") or r.status_code)[:160])}
    except Exception as e:
        return {"ok": False, "error": "请求异常：%s" % str(e)[:120]}


def telegram_get_me(ch_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """连通性自检：调 getMe 验证 Token 有效（不发消息）。"""
    token = str(ch_cfg.get("bot_token") or "").strip()
    if not token:
        return {"ok": False, "error": "Bot Token 未配置"}
    proxy = str(ch_cfg.get("proxy") or "").strip()
    try:
        import httpx
        kwargs = {"timeout": 20.0, "verify": False}
        if proxy:
            kwargs["proxy"] = proxy
        with httpx.Client(**kwargs) as client:
            r = client.get("https://api.telegram.org/bot%s/getMe" % token)
            d = r.json() if r.status_code == 200 else {}
        res = (d or {}).get("result") or {}
        if (d or {}).get("ok") is True:
            return {"ok": True, "username": res.get("username"), "id": res.get("id")}
        return {"ok": False, "error": str((d or {}).get("description") or r.status_code)[:160]}
    except Exception as e:
        return {"ok": False, "error": "请求异常：%s" % str(e)[:120]}


_SENDERS = {"wecom": _send_wecom, "telegram": _send_telegram}


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
        ch_cfg = cfg["channels"].get(key) or {}
        # 每渠道独立启用（MP 的 NotificationConf.enabled 语义）：
        # 渠道关闭则跳过（send_test 显式指定渠道不受此限）
        if not channel and not ch_cfg.get("_enabled", True):
            continue
        r = _SENDERS[key](ch_cfg, text)
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


def send_scene(scene: str, title: str, lines: List[str]) -> Dict[str, Any]:
    """按场景推送通知（渠道订阅过滤 + 总开关）。

    与 send() 的区别：send() 是「发给所有渠道」的通用推送；本函数按
    **场景订阅**过滤——渠道只收它订阅的场景（`_scenes` 空 = 全部场景）。
    返回 {ok, scene, results:{channel: result}, sent_to:[...]}。
    """
    cfg = _load()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "通知总开关未开启", "scene": scene}
    body = title + "\n" + "\n".join(lines) if lines else title
    results: Dict[str, Any] = {}
    sent_to: List[str] = []
    for c in CHANNELS:
        key = c["key"]
        if key not in _SENDERS:
            continue
        ch_cfg = cfg["channels"].get(key) or {}
        # 渠道级停用（MoviePilot 的 NotificationConf.enabled 语义）
        if not ch_cfg.get("_enabled", True):
            continue
        # 场景订阅：_scenes 非空且不含本场景 → 跳过
        scenes = ch_cfg.get("_scenes") or []
        if scenes and scene not in scenes:
            continue
        r = _SENDERS[key](ch_cfg, body)
        results[key] = r
        if r.get("ok"):
            sent_to.append(key)
    return {"ok": bool(sent_to), "scene": scene, "results": results,
            "sent_to": sent_to}


# ---------------------------------------------------------------------------
# 企业微信回调（接收消息服务器）：GET 验证 + POST 收消息 → 可回复。
#
# 流程（企微官方 WXBizMsgCrypt 协议）：
#   GET  /callback?msg_signature=..&timestamp=..&nonce=..&echostr=..
#        → 校验签名 → 用 AESKey 解密 echostr → 明文回显（启用验证）
#   POST /callback（body 为加密 XML）
#        → 校验签名 → 解密 → 提取 MsgType/Content/FromUserName →
#          生成回复（文本）→ 加密签名后返回 XML
#
# 安全：签名校验用 SHA1(sort(token, timestamp, nonce, encrypt))；
#       AES-256-CBC，Key = Base64Decode(AESKey + "=")；CorPID 作 IV 部分偏移。
#       任何校验失败回 403/empty —— 企微会重试，回调必须幂等。
# ---------------------------------------------------------------------------
def _wecom_check_sig(cb_token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    import hashlib
    raw = sorted([cb_token or "", timestamp or "", nonce or "", encrypt or ""])
    return hashlib.sha1("".join(raw).encode("utf-8")).hexdigest()


def _wecom_aes_key(aes_key43: str) -> bytes:
    import base64
    return base64.b64decode((aes_key43 or "") + "=")


def _wecom_decrypt(encrypt: str, aes_key43: str, corpid: str) -> Optional[str]:
    """解密企微消息体，返回明文 XML；失败返回 None。"""
    try:
        import base64
        from Crypto.Cipher import AES  # pycryptodome
        key = _wecom_aes_key(aes_key43)
        data = base64.b64decode(encrypt)
        cipher = AES.new(key, AES.MODE_CBC, key[:16])
        plain = cipher.decrypt(data)
        # PKCS7 去填充
        pad = plain[-1]
        if isinstance(pad, int) and 1 <= pad <= 32:
            plain = plain[:-pad]
        # 明文结构：16 字节随机 + 4 字节网络序长度 + 消息体 + receiveid(corpid)
        import struct
        msg_len = struct.unpack(">I", plain[16:20])[0]
        msg = plain[20:20 + msg_len].decode("utf-8", "replace")
        # 校验尾部 receiveid
        rvid = plain[20 + msg_len:].decode("utf-8", "replace")
        if corpid and rvid != corpid:
            return None
        return msg
    except Exception:
        return None


def _wecom_encrypt(plain_xml: str, aes_key43: str, corpid: str) -> str:
    """加密回复明文，返回 Base64 密文。"""
    import base64
    import os
    import struct
    from Crypto.Cipher import AES
    key = _wecom_aes_key(aes_key43)
    msg = plain_xml.encode("utf-8")
    body = os.urandom(16) + struct.pack(">I", len(msg)) + msg + corpid.encode("utf-8")
    pad = 32 - (len(body) % 32)
    body += bytes([pad]) * pad
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    return base64.b64encode(cipher.encrypt(body)).decode("ascii")


def _xml_pick(xml: str, tag: str) -> str:
    """从明文 XML 取某个标签的文本（兼容 CDATA）。"""
    import re
    m = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (tag, tag), xml, re.S)
    return m.group(1) if m else ""


def _xml_text_content(xml: str) -> tuple:
    """从明文 XML 提取 (MsgType, Content, FromUserName, EventKey)。"""
    return (_xml_pick(xml, "MsgType"), _xml_pick(xml, "Content"),
            _xml_pick(xml, "FromUserName"), _xml_pick(xml, "EventKey"))


def wecom_callback_verify(params: Dict[str, Any]) -> Optional[str]:
    """GET 验证：成功返回解密后的 echostr 明文，失败返回 None。"""
    try:
        cfg = _load().get("channels", {}).get("wecom") or {}
        cb_token = str(cfg.get("cb_token") or "").strip()
        aes_key = str(cfg.get("cb_aes_key") or "").strip()
        corpid = str(cfg.get("corpid") or "").strip()
        if not (cb_token and aes_key and corpid):
            return None
        sig = str(params.get("msg_signature") or "")
        ts = str(params.get("timestamp") or "")
        nonce = str(params.get("nonce") or "")
        echostr = str(params.get("echostr") or "")
        if not echostr:
            return None
        if _wecom_check_sig(cb_token, ts, nonce, echostr) != sig:
            return None
        return _wecom_decrypt(echostr, aes_key, corpid)
    except Exception:
        return None


def wecom_callback_message(params: Dict[str, Any], body_xml_encrypted: str) -> Optional[str]:
    """POST 收消息：返回加密签名的回复 XML（可直接作为响应体）；无需回复时返回 None（回空串）。

    交互路由（首版，文本消息）：
      - 「帮助/help」→ 指令清单
      - 「积分/余额」→ 汇总剩余（读 credit 快照，轻量本地）
      - 「签到」→ 触发一轮每日任务（异步启动，回执已受理）
      - 「任务」→ 触发全量任务（同上）
      - 其余 → 引导语
    """
    try:
        cfg = _load().get("channels", {}).get("wecom") or {}
        cb_token = str(cfg.get("cb_token") or "").strip()
        aes_key = str(cfg.get("cb_aes_key") or "").strip()
        corpid = str(cfg.get("corpid") or "").strip()
        if not (cb_token and aes_key and corpid):
            return None
        import re
        m = re.search(r"<Encrypt>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</Encrypt>", body_xml_encrypted, re.S)
        if not m:
            return None
        encrypt = m.group(1).strip()
        sig = str(params.get("msg_signature") or "")
        ts = str(params.get("timestamp") or "")
        nonce = str(params.get("nonce") or "")
        if _wecom_check_sig(cb_token, ts, nonce, encrypt) != sig:
            return None
        plain = _wecom_decrypt(encrypt, aes_key, corpid)
        if not plain:
            return None
        # 四元组：菜单点击是 event 消息，命令在 EventKey 里（无 Content）
        msg_type, content, from_user, event_key = _xml_text_content(plain)
        reply_text = _wecom_interpret(msg_type, content, event_key)
        if not reply_text:
            return None
        # 组回复 XML → 加密 → 包外层
        reply_xml = (
            "<xml>"
            f"<ToUserName><![CDATA[{from_user}]]></ToUserName>"
            f"<FromUserName><![CDATA[{corpid}]]></FromUserName>"
            f"<CreateTime><![CDATA[{int(time.time())}]]></CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType>"
            f"<Content><![CDATA[{reply_text}]]></Content>"
            "</xml>")
        enc2 = _wecom_encrypt(reply_xml, aes_key, corpid)
        import uuid
        import hashlib
        sig2 = hashlib.sha1("".join(sorted([cb_token, ts, nonce, enc2])).encode("utf-8")).hexdigest()
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{enc2}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{sig2}]]></MsgSignature>"
            f"<TimeStamp><![CDATA[{ts}]]></TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 交互命令路由 —— **唯一实现**（企微菜单/文本与 Telegram 共用）。
#
# 历史坑（2026-10-13 用户二次反馈「交互菜单还是没变」）：
#   web_proxy 的回调处理器里曾**内联一份** if/elif 命令链，与这里的
#   `_wecom_interpret` 是两份代码、各改一半 —— 改 notify.py 完全不生效，
#   因为回调实际走 web_proxy 那份（日志里 POST /callback 由 web_proxy 服务）。
# 现已收拢：web_proxy 只负责加解密，命令语义全部由本函数提供。
#
# 口径（2026-10-13 用户需求）：
#   · 账号余额 → **完整**列出全部账号积分（此前只给积分统计顶部四项）；
#   · 模型     → 「当前模型」+ 全量模型清单 + 轮检禁用/手动禁用分类。
# ---------------------------------------------------------------------------

_WEBUI_BASE = "http://127.0.0.1:18090"
_GW_BASE = "http://127.0.0.1:18091"


def _http_json(url: str, timeout: float = 8.0, method: str = "GET",
               body: Optional[bytes] = None) -> Any:
    """同步取本地 JSON（本模块跑在容器内，127.0.0.1 直连即可）。"""
    import urllib.request
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _cmd_help() -> str:
    return ("AutoBuddy 指令：\n"
            "· 账号余额 —— 全部账号积分明细\n"
            "· 积分消耗 —— 账号池积分汇总\n"
            "· 模型 —— 当前模型清单与禁用分类\n"
            "· 状态 —— 网关与账号池状态\n"
            "· 签到 —— 全部账号签到\n"
            "· 任务 —— 每日任务进度 / 启动\n"
            "· 通知 on/off —— 开关通知推送")


def _cmd_accounts() -> str:
    """账号余额：完整列出（用户要求「不能只显示积分统计顶部四项」）。"""
    d = _http_json(_WEBUI_BASE + "/api/credits/stats")
    accs = d.get("accounts") or []
    if not accs:
        return "暂无账号余额数据"
    lines = ["💰 账号余额（%d 个）" % len(accs)]
    total = 0.0
    for a in accs:
        name = str(a.get("accountName") or a.get("accountId") or "?")
        rem = a.get("currentRemaining")
        try:
            total += float(rem)
        except (TypeError, ValueError):
            pass
        lines.append("  %s: %s" % (name[:16], _fmt_num(rem)))
    lines.append("  合计：%s" % _fmt_num(total))
    return "\n".join(lines)


def _cmd_credits() -> str:
    d = _http_json(_WEBUI_BASE + "/api/credits/stats")
    s = d.get("summary") or {}
    return ("📊 账号池积分\n"
            "  · 剩余积分：%s\n"
            "  · 今日消耗：%s\n"
            "  · 近 7 天消耗：%s\n"
            "  · 本月消耗：%s" % (
                _fmt_num(s.get("currentRemaining")), _fmt_num(s.get("usageToday")),
                _fmt_num(s.get("usage7Days")), _fmt_num(s.get("usageThisMonth"))))


def _cmd_models() -> str:
    """当前模型：全量清单 + 轮检禁用/手动禁用分类。"""
    g = _http_json(_WEBUI_BASE + "/api/gateway-info")
    m = g.get("models") or {}
    src = g.get("disabledBySource") or {}
    ids: list = []
    try:
        am = _http_json(_WEBUI_BASE + "/api/account-models")
        ids = [str(x) for x in (am.get("catalog") or []) if x]
    except Exception:
        pass
    if not ids:
        ids = [str(x) for x in (m.get("sample") or []) if x]
    lines = [
        "🤖 当前模型（%s 个）" % m.get("count", "?"),
        "  · 自动发现：%s 个" % m.get("autoDiscovered", "?"),
        "  · 轮检禁用：%s 个组合" % (src.get("auto") or 0),
        "  · 手动禁用：%s 个组合" % (src.get("manual") or 0),
    ]
    if ids:
        lines.append("  · 模型清单（%d 个）：" % len(ids))
        for i in range(0, len(ids), 5):
            lines.append("    " + ", ".join(ids[i:i + 5]))
    return "\n".join(lines)


def _cmd_status() -> str:
    h = _http_json(_GW_BASE + "/health")
    info = _http_json(_WEBUI_BASE + "/api/gateway-info")
    acc_n = ((info.get("accounts") or {}).get("total") or "?")
    return ("AutoBuddy v%s\n账号池: %s 个账号\n模型: %s 个\n状态: %s" % (
        h.get("version", "?"), acc_n,
        h.get("models_count", "?"), h.get("status", "?")))


def _cmd_daily() -> str:
    d = _http_json(_WEBUI_BASE + "/api/wb-daily/jobs")
    jobs = d.get("jobs") or []
    if not jobs:
        return "暂无任务记录，回复「任务」启动一轮"
    j = jobs[0]
    return ("最近任务: %s（%s）\n进度: %s 账号 %s/%s" % (
        j.get("job_id"), j.get("status"), j.get("progress"),
        j.get("accounts_done"), j.get("accounts_total")))


def _cmd_run() -> str:
    d = _http_json(_WEBUI_BASE + "/api/wb-daily/run", method="POST", body=b"")
    if d.get("job_id"):
        return "每日任务已启动（%s），完成后会推送结果" % d["job_id"]
    return "启动失败：%s" % (d.get("detail") or "已有任务在执行")


def _cmd_checkin() -> str:
    d = _http_json(_WEBUI_BASE + "/api/checkin/all", method="POST", body=b"{}")
    return "签到已提交：%s" % (d.get("status") or "已开始")


def wecom_command_reply(cmd: str) -> str:
    """命令 → 回复文本。企微与 Telegram 共用的**唯一**命令语义实现。"""
    text = (cmd or "").strip()
    if not text:
        return "（空消息）"
    orig = text.lower().lstrip("/").split("@", 1)[0].strip()
    low = orig
    _EN = {"start": "help", "notify_on": "notify on", "notify_off": "notify off"}
    if low in _EN:
        low = _EN[low]
    _EN2ZH = {"status": "状态", "credits": "积分", "balance": "积分",
              "accounts": "账号", "models": "模型",
              "daily": "任务", "run": "任务", "checkin": "签到", "help": "help"}
    if low in _EN2ZH:
        text = low = _EN2ZH[low]

    if low.startswith("notify"):
        arg = low.split()[-1] if " " in low else ""
        if arg in ("on", "开"):
            update_config({"enabled": True})
            return "通知已开启"
        if arg in ("off", "关"):
            update_config({"enabled": False})
            return "通知已关闭"
        v = public_view()
        return "通知当前：%s" % ("开启" if v.get("enabled") else "关闭")

    if low in ("help", "帮助", "?", "？", "菜单"):
        return _cmd_help()
    try:
        if "账号" in text or orig in ("accounts", "balance"):
            return _cmd_accounts()
        if "模型" in text or orig in ("models",):
            return _cmd_models()
        if "积分" in text or "余额" in text or orig in ("credits",):
            return _cmd_credits()
        if "状态" in text or orig in ("status",):
            return _cmd_status()
        if "签到" in text or orig in ("checkin",):
            return _cmd_checkin()
        if "任务" in text or orig in ("daily", "run"):
            if orig == "daily":
                return _cmd_daily()
            return _cmd_run()
    except Exception:
        return "数据读取失败，请稍后再试"
    return "收到：%s\n（回复「帮助」查看可用指令）" % text[:60]


def _wecom_interpret(msg_type: str, content: str, event_key: str = "") -> Optional[str]:
    """把收到的消息翻译成回复文本（命令语义统一走 `wecom_command_reply`）。

    支持两种来源：文本消息（命令在 content）与菜单点击（命令在 event_key）。
    """
    if event_key:
        content = MENU_KEY_TO_CMD.get(str(event_key).strip().upper(), "")
        msg_type = "text"
        if not content:
            return "未知菜单操作：%s" % str(event_key)[:40]
    if msg_type != "text":
        return "我是 AutoBuddy 网关助手。发「帮助」或点下方菜单查看我能做什么～"
    return wecom_command_reply(content or "")


# ---------------------------------------------------------------------------
# 企微回调（接收消息服务器）：echo 校验 + 消息接收（交互功能的底座）。
#
# 企微协议：AES-256-CBC（key = Base64(EncodingAESKey + "=")，43 字符 AESKey
# 解码为 32 字节），PKCS7 填充，明文 = 16 字节随机 + 4 字节网络序消息长度 +
# 明文消息 + 收到的 corpid。签名 = SHA1(sorted(token, timestamp, nonce, 密文))。
# 容器内无 pycryptodome/cryptography，用 openssl CLI（已实测往返一致）。
# 回调是低频路径（成员发消息才触发），subprocess 开销可接受。
# ---------------------------------------------------------------------------
import base64
import hashlib
import secrets
import struct
import subprocess
import xml.etree.ElementTree as ET


def _aes_key_b64(aes_key: str) -> bytes:
    """43 字符 EncodingAESKey + '=' 的 Base64 即 32 字节 AES key。"""
    return base64.b64decode((aes_key.strip() + "=").encode())


# ⚠️ 企微官方加解密协议的 PKCS7 填充块是 **32 字节**（官方 WXBizMsgCrypt
# 的 block_size=32），不是 AES 分组本身的 16。此前误写 16，本地往返自洽
# 测不出来，但真实企微消息按 32 字节块填充，按 16 校验必然
# 「bad pkcs7 padding」（2026-10-13 真实回调实测报错）。
_AES_BLOCK = 32


def _pkcs7_pad(data: bytes) -> bytes:
    # 企微：即使 data 已是 32 的倍数也要补一整块（官方实现语义）
    n = _AES_BLOCK - (len(data) % _AES_BLOCK)
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes) -> bytes:
    n = data[-1]
    if n < 1 or n > _AES_BLOCK:
        raise ValueError("bad pkcs7 padding")
    # 校验整段填充字节一致，防止误判（企微明文末尾恰好是合法值时不会误剥）
    if data[-n:] != bytes([n]) * n:
        raise ValueError("bad pkcs7 padding")
    return data[:-n]


def _openssl_cbc(data: bytes, key: bytes, iv: bytes, enc: bool) -> bytes:
    """AES-256-CBC 加/解密（openssl 子进程，key/iv 均为 32/16 字节）。"""
    # ⚠️ 必须 -nosalt -nopad：openssl 默认会加/剥自己的 PKCS7 填充并带 salt 头，
    # 那会与企微的「无 salt + 标准 PKCS7」格式冲突 —— 表现为解密后再剥一次填充
    # 时报 bad pkcs7 padding（真实故障，2026-10-13）。填充统一由本模块处理。
    args = ["openssl", "enc", "-aes-256-cbc", "-nosalt", "-nopad",
            "-K", key.hex(), "-iv", iv.hex()]
    if not enc:
        args.append("-d")
    p = subprocess.run(args, input=data, capture_output=True, check=True)
    return p.stdout


def _wecom_sign(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypted])).encode()).hexdigest()


def _aes_decrypt(encrypted_b64: str, aes_key: str) -> tuple:
    """解密企微密文 → (xml 明文, 收到的 corpid)。失败抛异常。"""
    key = _aes_key_b64(aes_key)
    blob = base64.b64decode(encrypted_b64)
    plain = _pkcs7_unpad(_openssl_cbc(blob, key, key[:16], enc=False))
    msg_len = struct.unpack(">I", plain[16:20])[0]
    msg = plain[20:20 + msg_len].decode("utf-8")
    corp = plain[20 + msg_len:].decode("utf-8")
    return msg, corp


def _aes_encrypt(plain_xml: str, aes_key: str, corpid: str) -> str:
    """加密回复：16 随机字节 + 长度 + xml + corpid → AES → Base64。"""
    key = _aes_key_b64(aes_key)
    payload = plain_xml.encode("utf-8")
    blob = secrets.token_bytes(16) + struct.pack(">I", len(payload)) + payload + corpid.encode("utf-8")
    return base64.b64encode(_openssl_cbc(_pkcs7_pad(blob), key, key[:16], enc=True)).decode()


def verify_echo(token: str, aes_key: str, corpid: str,
                encrypted: str, msg_sign: str, timestamp: str, nonce: str) -> str:
    """GET 校验（企微保存回调配置时调用）：验签 → 解密 echostr → 回明文。"""
    if _wecom_sign(token, timestamp, nonce, encrypted) != msg_sign:
        raise ValueError("签名校验失败（检查 Token 是否一致）")
    plain, _corp = _aes_decrypt(encrypted, aes_key)
    return plain


def decrypt_message(token: str, aes_key: str, corpid: str,
                    encrypted: str, msg_sign: str, timestamp: str, nonce: str) -> str:
    """POST 消息解密：验签 → 解密 → 返回 xml 明文（调用方再解析）。"""
    if _wecom_sign(token, timestamp, nonce, encrypted) != msg_sign:
        raise ValueError("签名校验失败")
    xml_text, recv_corp = _aes_decrypt(encrypted, aes_key)
    if corpid and recv_corp and corpid != recv_corp:
        raise ValueError("corpid 不匹配")
    return xml_text


def encrypt_reply(reply_xml: str, token: str, aes_key: str, corpid: str,
                  timestamp: str, nonce: str) -> str:
    """构造加密回复的完整 XML（企微要求的响应格式）。"""
    enc = _aes_encrypt(reply_xml, aes_key, corpid)
    sign = _wecom_sign(token, timestamp, nonce, enc)
    return ('<xml>'
            '<Encrypt><![CDATA[%s]]></Encrypt>'
            '<MsgSignature><![CDATA[%s]]></MsgSignature>'
            '<TimeStamp>%s</TimeStamp>'
            '<Nonce><![CDATA[%s]]></Nonce>'
            '</xml>') % (enc, sign, timestamp, nonce)


def parse_message(xml_text: str) -> Dict[str, str]:
    """解析企微消息 xml → dict（MsgType/Content/FromUserName 等）。"""
    root = ET.fromstring(xml_text)
    return {child.tag: (child.text or "") for child in root}


def build_reply_text(to_user: str, from_user: str, content: str) -> str:
    """构造被动回复的明文 xml（text 类型）。"""
    import time as _t
    return ('<xml>'
            '<ToUserName><![CDATA[%s]]></ToUserName>'
            '<FromUserName><![CDATA[%s]]></FromUserName>'
            '<CreateTime>%d</CreateTime>'
            '<MsgType><![CDATA[text]]></MsgType>'
            '<Content><![CDATA[%s]]></Content>'
            '</xml>') % (to_user, from_user, int(_t.time()), content)


# ---------------------------------------------------------------------------
# 企微自定义菜单（应用底部按钮）—— MoviePilot 的 create_menus 同款能力。
#
# 为什么必须有它：只靠「用户手打命令」不算交互 —— 用户根本不知道能打什么。
# MoviePilot 的做法是配置保存后**自动调 menu/create 注册菜单**，成员点按钮
# 即触发（企微把按钮 key 作为 EventKey 回调过来）。本模块照做：
#   - 限企微协议上限：一级菜单最多 3 个，每个一级下二级最多 5 个；
#   - 按钮 type=click，key 即命令（AB_STATUS 等），点击后走同一套命令路由；
#   - create_wecom_menus() 幂等：重复调用是覆盖式更新，不会堆积。
# ---------------------------------------------------------------------------

# 命令 -> (显示名, 分组)。分组即一级菜单（<=3 组，每组 <=5 项）。
WECOM_MENU_COMMANDS = [
    ("AB_STATUS",   "网关状态",   "常用"),
    ("AB_CREDITS",  "积分消耗",   "常用"),
    ("AB_ACCOUNTS", "账号余额",   "常用"),
    ("AB_MODELS",   "模型数量",   "常用"),
    ("AB_DAILY",    "任务进度",   "任务"),
    ("AB_RUN",      "启动任务",   "任务"),
    ("AB_CHECKIN",  "全部签到",   "任务"),
    ("AB_NOTIFY_ON", "开启通知",  "通知"),
    ("AB_NOTIFY_OFF", "关闭通知", "通知"),
    ("AB_HELP",     "帮助",       "通知"),
]

# 菜单按钮 key -> 交互命令（与文本命令同一套路由，点按钮等价于手打该命令）
MENU_KEY_TO_CMD = {
    "AB_STATUS": "status",
    "AB_CREDITS": "credits",
    "AB_ACCOUNTS": "accounts",
    "AB_MODELS": "models",
    "AB_DAILY": "daily",
    "AB_RUN": "run",
    "AB_CHECKIN": "checkin",
    "AB_NOTIFY_ON": "notify on",
    "AB_NOTIFY_OFF": "notify off",
    "AB_HELP": "help",
}


def wecom_menu_spec() -> Dict[str, Any]:
    """按分组构造企微菜单报文（自动受 3 个一级 / 5 个二级限制）。"""
    groups: Dict[str, list] = {}
    for key, name, cat in WECOM_MENU_COMMANDS:
        groups.setdefault(cat, []).append({"type": "click", "name": name, "key": key})
    buttons = []
    for cat, items in groups.items():
        if len(items) == 1:
            buttons.append(items[0])          # 只有一项时直接做一级按钮
        else:
            buttons.append({"name": cat, "sub_button": items[:5]})
    return {"button": buttons[:3]}


def _wecom_token(ch_cfg: Dict[str, Any]) -> tuple:
    """取 (access_token, agentid_num)；失败返回 (None, err)。"""
    corpid = str(ch_cfg.get("corpid") or "").strip()
    secret = str(ch_cfg.get("secret") or "").strip()
    agentid = str(ch_cfg.get("agentid") or "").strip()
    if not (corpid and secret and agentid):
        return None, "企业微信配置不完整（CorPID/Secret/AgentId）"
    try:
        agentid_num = int(agentid)
    except ValueError:
        return None, "AgentId 必须是纯数字"
    try:
        import httpx
        with httpx.Client(timeout=20.0, verify=False) as client:
            r = client.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                           params={"corpid": corpid, "corpsecret": secret})
            d = r.json() if r.status_code == 200 else {}
        token = (d or {}).get("access_token")
        if not token:
            return None, "获取 access_token 失败：errcode=%s %s" % (
                (d or {}).get("errcode"), _sanitize_token(str((d or {}).get("errmsg") or ""))[:120])
        return (token, agentid_num), None
    except Exception as e:
        return None, "请求异常：%s" % str(e)[:120]


def create_wecom_menus(channel_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """注册/更新企微自定义菜单（幂等）。配置就绪即可调用。"""
    cfg = channel_cfg or (_load().get("channels", {}).get("wecom") or {})
    got, err = _wecom_token(cfg)
    if err:
        return {"ok": False, "error": err}
    token, agentid = got
    try:
        import httpx
        r = httpx.post(
            "https://qyapi.weixin.qq.com/cgi-bin/menu/create",
            params={"access_token": token, "agentid": agentid},
            json=wecom_menu_spec(), timeout=20.0, verify=False)
        d = r.json() if r.status_code == 200 else {}
        if (d or {}).get("errcode") == 0:
            return {"ok": True, "buttons": len(wecom_menu_spec()["button"])}
        return {"ok": False, "error": "创建菜单失败：errcode=%s %s" % (
            (d or {}).get("errcode"), _sanitize_token(str((d or {}).get("errmsg") or ""))[:160])}
    except Exception as e:
        return {"ok": False, "error": "请求异常：%s" % str(e)[:120]}


def delete_wecom_menus(channel_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """删除企微自定义菜单。"""
    cfg = channel_cfg or (_load().get("channels", {}).get("wecom") or {})
    got, err = _wecom_token(cfg)
    if err:
        return {"ok": False, "error": err}
    token, agentid = got
    try:
        import httpx
        r = httpx.get(
            "https://qyapi.weixin.qq.com/cgi-bin/menu/delete",
            params={"access_token": token, "agentid": agentid}, timeout=20.0, verify=False)
        d = r.json() if r.status_code == 200 else {}
        return {"ok": (d or {}).get("errcode") == 0,
                "error": None if (d or {}).get("errcode") == 0 else str((d or {}).get("errmsg"))[:120]}
    except Exception as e:
        return {"ok": False, "error": "请求异常：%s" % str(e)[:120]}


# ---------------------------------------------------------------------------
# Telegram 交互：命令菜单自动注册 + getUpdates 长轮询（照 MoviePilot 实现）。
#
# MoviePilot 的做法（app/modules/telegram/telegram.py）：
#   - register_commands()：delete_my_commands() 后 set_my_commands()，
#     配置保存即自动在客户端输入框左侧生成命令菜单（点「/」就能选）；
#   - 收消息用 **getUpdates 长轮询**（不是 webhook）—— 因此 Telegram 通道
#     **不需要公网入口**，这点与企微回调不同（我此前错误地把「需公网」
#     套到了 Telegram 上，这里更正）。
#
# 命令表复用企微菜单的 MENU_KEY_TO_CMD（同一套命令，两种通道一致），
# 命令名取小写（Telegram 命令只能小写字母/数字/下划线）。
# ---------------------------------------------------------------------------

# Telegram 命令 -> 描述（用于 setMyCommands 菜单展示）
TELEGRAM_COMMANDS = [
    ("status",   "网关与账号池状态"),
    ("credits",  "积分与今日消耗"),
    ("accounts", "账号余额清单"),
    ("models",   "模型数量"),
    ("daily",    "每日任务进度"),
    ("run",      "启动一轮每日任务"),
    ("checkin",  "全部账号签到"),
    ("help",     "可用指令清单"),
]

_TG_POLL_THREAD = None
_TG_POLL_STOP = None


def _tg_api(token: str, proxy: str = "") -> Any:
    """构造 Telegram 客户端（渠道级代理，不影响全局）。"""
    import httpx
    kwargs = {"timeout": 40.0, "verify": False}
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def telegram_register_commands(ch_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """注册/刷新 Telegram 命令菜单（幂等）。配置就绪即可调用。"""
    cfg = ch_cfg or (_load().get("channels", {}).get("telegram") or {})
    token = str(cfg.get("bot_token") or "").strip()
    if not token:
        return {"ok": False, "error": "Bot Token 未配置"}
    proxy = str(cfg.get("proxy") or "").strip()
    try:
        with _tg_api(token, proxy) as client:
            client.post("https://api.telegram.org/bot%s/deleteMyCommands" % token)
            r = client.post(
                "https://api.telegram.org/bot%s/setMyCommands" % token,
                json={"commands": [{"command": c, "description": d}
                                   for c, d in TELEGRAM_COMMANDS]})
            d = r.json() if r.status_code == 200 else {}
        if (d or {}).get("ok") is True:
            return {"ok": True, "commands": len(TELEGRAM_COMMANDS)}
        return {"ok": False, "error": "注册命令失败：%s" % _sanitize_token(
            str((d or {}).get("description") or r.status_code)[:160])}
    except Exception as e:
        return {"ok": False, "error": "请求异常：%s" % str(e)[:120]}


def telegram_delete_commands(ch_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """清理 Telegram 命令菜单。"""
    cfg = ch_cfg or (_load().get("channels", {}).get("telegram") or {})
    token = str(cfg.get("bot_token") or "").strip()
    if not token:
        return {"ok": False, "error": "Bot Token 未配置"}
    proxy = str(cfg.get("proxy") or "").strip()
    try:
        with _tg_api(token, proxy) as client:
            r = client.post("https://api.telegram.org/bot%s/deleteMyCommands" % token)
            d = r.json() if r.status_code == 200 else {}
        return {"ok": (d or {}).get("ok") is True}
    except Exception as e:
        return {"ok": False, "error": "请求异常：%s" % str(e)[:120]}


def _tg_reply(token: str, proxy: str, chat_id: str, text: str) -> None:
    with _tg_api(token, proxy) as client:
        client.post("https://api.telegram.org/bot%s/sendMessage" % token,
                    json={"chat_id": chat_id, "text": text[:4000]})


def _tg_run_polling(stop_event: Any) -> None:
    """长轮询主循环：拉 getUpdates → 解释命令 → 回消息。

    照 MoviePilot：串行处理、失败指数退避、handler 抛错也确认 offset（避免同一条
    坏消息反复投递）。命令路由复用 _wecom_interpret（企微菜单同套命令）。
    """
    offset = None
    failures = 0
    while not stop_event.is_set():
        try:
            cfg = _load().get("channels", {}).get("telegram") or {}
            token = str(cfg.get("bot_token") or "").strip()
            proxy = str(cfg.get("proxy") or "").strip()
            if not token:
                stop_event.wait(30)
                continue
            with _tg_api(token, proxy) as client:
                r = client.post(
                    "https://api.telegram.org/bot%s/getUpdates" % token,
                    json={"offset": offset, "timeout": 30,
                          "allowed_updates": ["message", "callback_query"]})
                d = r.json() if r.status_code == 200 else {}
            updates = (d or {}).get("result") or []
            failures = 0
            for up in updates:
                try:
                    offset = max(offset or 0, int(up.get("update_id") or 0)) + 1
                except Exception:
                    pass
                try:
                    msg = up.get("message") or {}
                    call = up.get("callback_query") or {}
                    if call:
                        chat_id = str(((call.get("message") or {}).get("chat") or {}).get("id") or "")
                        data = str(call.get("data") or "").strip()
                        if chat_id and data:
                            reply = _wecom_interpret("text", MENU_KEY_TO_CMD.get(data.upper(), data.lower())) or "未知操作"
                            _tg_reply(token, proxy, chat_id, reply)
                        continue
                    chat_id = str((msg.get("chat") or {}).get("id") or "")
                    text = str(msg.get("text") or "").strip()
                    if not chat_id or not text:
                        continue
                    # 命令形态：/status 或 /status@bot → 取命令名
                    cmd_text = text
                    if text.startswith("/"):
                        cmd_text = text.split(maxsplit=1)[0][1:].split("@", 1)[0]
                    reply = _wecom_interpret("text", cmd_text)
                    if reply:
                        _tg_reply(token, proxy, chat_id, reply)
                except Exception:
                    # handler 抛错只记录，仍确认该更新（照 MoviePilot 语义）
                    continue
        except Exception:
            failures += 1
            stop_event.wait(min(3 * 2 ** (failures - 1), 60))


def start_telegram_polling() -> bool:
    """启动 Telegram 长轮询（后台守护线程，幂等）。配置就绪才真正开跑。"""
    global _TG_POLL_THREAD, _TG_POLL_STOP
    try:
        if _TG_POLL_THREAD is not None and _TG_POLL_THREAD.is_alive():
            return True
        cfg = _load().get("channels", {}).get("telegram") or {}
        if not str(cfg.get("bot_token") or "").strip():
            return False
        _TG_POLL_STOP = threading.Event()
        _TG_POLL_THREAD = threading.Thread(
            target=_tg_run_polling, args=(_TG_POLL_STOP,),
            name="ab-tg-polling", daemon=True)
        _TG_POLL_THREAD.start()
        return True
    except Exception:
        return False


def stop_telegram_polling() -> None:
    global _TG_POLL_STOP
    try:
        if _TG_POLL_STOP is not None:
            _TG_POLL_STOP.set()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 场景内容组装器（2026-10-13 按用户需求实现 5 类通知）。
#
# 每个 builder 返回 (title, [lines...])，由调用方经 send_scene() 推送。
# 设计取舍：组装器只读本地数据（DB / 账本 / 配置），不做网络调用 ——
# 慢路径交给调用方异步执行，避免阻塞签到/巡检主流程。
# ---------------------------------------------------------------------------

def _fmt_num(v) -> str:
    try:
        f = float(v or 0)
        return ("{:,.2f}".format(f)).rstrip("0").rstrip(".") if f != int(f) else "{:,.0f}".format(f)
    except Exception:
        return str(v)


def build_daily_report(yesterday_credits: Dict[str, Any],
                       api_keys_stats: Dict[str, Any],
                       models_info: Dict[str, Any]) -> tuple:
    """① 每日凌晨 0 点报告（用户明确：今日消耗改为**昨日消耗**）。

    yesterday_credits: {"date": "2026-10-12", "checkin": x, "task": y, "total": z,
                        "totalCredits": 总积分, "remaining": 剩余积分}
    api_keys_stats:    {"keyCount": n, "totalCalls": 累计, "todayCalls": 当天}
    models_info:       {"total": n, "autoDiscovered": m, "disabled": k, "manual": j}
    """
    d = str(yesterday_credits.get("date") or "昨日")
    lines = [
        "📅 报告日期：%s（凌晨 0 点推送）" % d,
        "",
        "💰 积分四项",
        "  · 昨日签到积分：%s" % _fmt_num(yesterday_credits.get("checkin")),
        "  · 昨日任务积分：%s" % _fmt_num(yesterday_credits.get("task")),
        "  · 昨日消耗合计：%s" % _fmt_num(yesterday_credits.get("total")),
        "  · 累计任务积分：%s" % _fmt_num(yesterday_credits.get("totalCredits")),
        "  · 账号池剩余积分：%s" % _fmt_num(yesterday_credits.get("remaining")),
        "",
        "🤖 模型",
        "  · 可用模型总数：%s（自动发现 %s）" % (
            models_info.get("total", "?"), models_info.get("autoDiscovered", "?")),
        "  · 轮检禁用：%s · 手动禁用：%s" % (
            models_info.get("disabled", "?"), models_info.get("manual", "?")),
        "",
        "🔌 API 调用",
        "  · 密钥数：%s" % api_keys_stats.get("keyCount", "?"),
        "  · 当天调用：%s 次" % api_keys_stats.get("todayCalls", "?"),
        "  · 累计调用：%s 次" % api_keys_stats.get("totalCalls", "?"),
    ]
    return ("🌅 AutoBuddy 每日报告（%s）" % d, lines)


def build_checkin_done(checkin_credits: float, remaining: float,
                       total: float) -> tuple:
    """② 签到成功：签到积分 + 账号池剩余 + 累计任务积分。

    ⚠️ 口径（2026-10-13 用户反馈「通知里剩余积分大于总积分」）：
    `remaining` 是账号池**实时余额**（credit_floor 快照合计，约 4 万），
    `total` 是台账里**累计做任务获得的积分**（约 3 万）——两者量纲不同，
    并列成「剩余 / 总」必然出现「剩余 > 总」的悖论。

    改为各自带明确归属：余额归账号池，累计归任务台账，不再并排比较。
    """
    lines = [
        "✅ 签到成功",
        "  · 本次签到积分：%s" % _fmt_num(checkin_credits),
        "  · 账号池剩余积分：%s" % _fmt_num(remaining),
        "  · 累计任务积分：%s" % _fmt_num(total),
    ]
    return ("✅ 签到完成", lines)


def build_task_done(task_credits: float, total: float,
                    summary: Optional[Dict[str, Any]] = None) -> tuple:
    """③ 每日任务成功：任务积分 + 累计任务积分。

    同②：`total` 是台账累计（非账号池余额），故称「累计任务积分」。
    """
    lines = [
        "🎯 每日任务执行完成",
        "  · 本次任务积分：%s" % _fmt_num(task_credits),
        "  · 累计任务积分：%s" % _fmt_num(total),
    ]
    if isinstance(summary, dict):
        if summary.get("accounts") is not None:
            lines.append("  · 参与账号：%s" % summary.get("accounts"))
        if summary.get("done") is not None:
            lines.append("  · 已完成：%s" % summary.get("done"))
    return ("🎯 每日任务完成", lines)


def build_health_done(result: Dict[str, Any], models_info: Dict[str, Any]) -> tuple:
    """④ 巡检结束：结果摘要 + 当前可用模型。"""
    checked = (result or {}).get("checked")
    disabled = (result or {}).get("disabled") or []
    enabled = (result or {}).get("enabled") or []
    lines = [
        "🔍 模型可用性巡检完成",
        "  · 本轮检测：%s 个组合" % (checked if checked is not None else "?"),
        "  · 新禁用：%s · 恢复启用：%s" % (len(disabled), len(enabled)),
        "",
        "🤖 当前模型",
        "  · 可用总数：%s（自动发现 %s）" % (
            models_info.get("total", "?"), models_info.get("autoDiscovered", "?")),
        "  · 轮检禁用：%s · 手动禁用：%s" % (
            models_info.get("disabled", "?"), models_info.get("manual", "?")),
    ]
    return ("🔍 巡检结果", lines)


def build_model_change(added: List[str], removed: List[str],
                       models_info: Dict[str, Any]) -> tuple:
    """⑤ 上游模型变化：变化明细 + 当前可用模型。"""
    lines = ["📦 上游模型目录变化"]
    if added:
        lines.append("  · ➕ 新增 %d 个：%s" % (len(added), ", ".join(added[:12])))
    if removed:
        lines.append("  · ➖ 下线 %d 个：%s" % (len(removed), ", ".join(removed[:12])))
    if not added and not removed:
        lines.append("  · 无变化")
    lines += [
        "",
        "🤖 当前可用",
        "  · 总数：%s（自动发现 %s）" % (
            models_info.get("total", "?"), models_info.get("autoDiscovered", "?")),
    ]
    return ("📦 模型目录变化", lines)
