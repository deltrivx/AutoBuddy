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
             "placeholder": "企微后台接收消息页的 Token"},
            {"key": "cb_aes_key", "label": "回调 EncodingAESKey", "secret": False,
             "placeholder": "43 位，企微后台生成"},
            # 回调模式（接收消息服务器）：配齐 cb_token/cb_aes_key/cb_url 后可在
            # 企微后台启用「API 接收」（这也是解锁「企业可信 IP」前置的两种方式之一）；
            # 收到成员消息后可回复，实现双向交互。
            {"key": "cb_token", "label": "回调 Token", "secret": True,
             "placeholder": "接收消息服务器的 Token（企微后台生成/自定义）"},
            {"key": "cb_aes_key", "label": "回调 EncodingAESKey", "secret": True,
             "placeholder": "43 位加密密钥（企微后台生成）"},
            {"key": "cb_url", "label": "回调 URL", "secret": False,
             "placeholder": "https://autobuddy.deltrivx.com/api/notify/wecom/callback"},
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


def _xml_text_content(xml: str) -> tuple:
    """从明文 XML 提取 (MsgType, Content, FromUserName)。"""
    import re
    def _pick(tag):
        m = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (tag, tag), xml, re.S)
        return m.group(1) if m else ""
    return _pick("MsgType"), _pick("Content"), _pick("FromUserName")


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
        msg_type, content, from_user = _xml_text_content(plain)
        reply_text = _wecom_interpret(msg_type, content)
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


def _wecom_interpret(msg_type: str, content: str) -> Optional[str]:
    """把收到的消息翻译成回复文本。仅文本消息；其余类型回引导。"""
    text = (content or "").strip()
    if msg_type != "text":
        return "我是 AutoBuddy 网关助手。发「帮助」查看我能做什么～"
    low = text.lower()
    if low in ("help", "帮助", "?", "？"):
        return ("AutoBuddy 指令：\n"
                "· 积分 / 余额 —— 查看账号池积分汇总\n"
                "· 签到 —— 立即跑一轮签到（仅签到）\n"
                "· 任务 —— 跑一轮全量每日任务\n"
                "· 状态 —— 查看网关与任务状态")
    if low in ("积分", "余额", "credit"):
        try:
            import credit_floor
            # 快照含全部账号最新余额：直接给总和与账号数
            snaps = credit_floor._latest_balances()
            if not snaps:
                return "暂无积分快照（约 5 小时采集一次），稍后再试。"
            total = sum(v for v in snaps.values() if isinstance(v, (int, float)))
            return "账号池 {} 个账号，剩余积分合计约 {:,.0f}。".format(len(snaps), total)
        except Exception:
            return "积分汇总读取失败，请稍后再试。"
    if "签到" in text or "任务" in text:
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://127.0.0.1:18090/api/wb-daily/run", method="POST", data=b"")
            urllib.request.urlopen(req, timeout=8)
            return "已启动一轮每日任务（签到+成长任务），完成后会推送结果。"
        except Exception:
            return "任务启动失败：可能已有任务在跑，稍后再试。"
    if "状态" in text:
        try:
            import urllib.request
            r = urllib.request.urlopen("http://127.0.0.1:18091/health", timeout=8)
            d = json.loads(r.read())
            return ("网关状态：{} · 模型 {} 个 · 账号池正常".format(
                d.get("status"), d.get("models_count")))
        except Exception:
            return "状态读取失败，请稍后再试。"
    return "收到：{}。发「帮助」查看可用指令～".format(text[:60])


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


def _pkcs7_pad(data: bytes) -> bytes:
    n = 32 - (len(data) % 32)
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes) -> bytes:
    n = data[-1]
    if n < 1 or n > 32:
        raise ValueError("bad pkcs7 padding")
    return data[:-n]


def _openssl_cbc(data: bytes, key: bytes, iv: bytes, enc: bool) -> bytes:
    """AES-256-CBC 加/解密（openssl 子进程，key/iv 均为 32/16 字节）。"""
    args = ["openssl", "enc", "-aes-256-cbc",
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
