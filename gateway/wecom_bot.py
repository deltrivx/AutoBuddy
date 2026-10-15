"""企业微信智能机器人通道（WebSocket 长连接）。

参考 MoviePilot `app/modules/wechat/wechatbot.py` 实现 —— 它的「MP影视助手」
正是靠这条链路收消息 / 发消息，而我们此前用的 HTTP `message/send` 会被企微
「可信 IP」校验拦住（errcode=60020）。

### 为什么改用 WebSocket

HTTP 应用消息（`cgi-bin/message/send`）要求调用方出口 IP 在企微后台
「管理工具 → 可信 IP」名单内。我们的容器出口是 NAT 池
（112.194.50.154 / .151 / .1 轮换），不在名单里，于是：

  - 日报、任务通知、助手延后答复**全部** errcode=60020；
  - 用户侧看到「助手只回『正在查询』然后没下文」。

而「智能机器人」是**出站**长连接（`wss://openws.work.weixin.qq.com`），
不校验来源 IP —— 这也正是 MP 的助手能正常工作的原因。

### 协议（对齐 MP 实测）

  - `aibot_subscribe` 订阅（body: bot_id / secret）
  - `aibot_msg_callback` 收消息（body.from.userid / text / msgtype）
  - `aibot_send_msg` 发消息（chatid / chat_type / msgtype=markdown）
  - `ping` 心跳 30s；`req_id` 做 ACK 配对，超时 10s 判失败
  - 单条消息按 4000 字节分片
  - 断线重连退避 [1, 2, 5, 10, 30, 60] 秒

### 凭据来源

企微管理后台 → 应用管理 → 对应的「智能机器人」页，给出机器人 ID 与 Secret
（与自建应用的 CorPID/Secret **不是**同一组）。

配置字段（notify 渠道 wecom 下）：`bot_id` / `bot_secret` / `bot_chat_id`。
两者可并存：配了 bot_* 就走 WebSocket 发送，未配则回落到 HTTP 通道。
"""

import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("wecom-bot")

WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL = 30
ACK_TIMEOUT = 10
CHUNK_BYTES = 4000
RECONNECT_DELAYS = [1, 2, 5, 10, 30, 60]


def _req_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def _split_content(content: str, max_bytes: int = CHUNK_BYTES) -> List[str]:
    """按字节分片，避免切断多字节字符（企微单条消息有长度上限）。"""
    if not content:
        return []
    chunks: List[str] = []
    current = bytearray()
    for line in content.splitlines():
        encoded = (line + "\n").encode("utf-8")
        if len(encoded) > max_bytes:
            if current:
                chunks.append(current.decode("utf-8", "replace").strip())
                current = bytearray()
            start = 0
            while start < len(encoded):
                end = min(start + max_bytes, len(encoded))
                while end > start and end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
                    end -= 1
                chunks.append(encoded[start:end].decode("utf-8", "replace").strip())
                start = end
            continue
        if len(current) + len(encoded) > max_bytes:
            chunks.append(current.decode("utf-8", "replace").strip())
            current = bytearray()
        current += encoded
    if current:
        chunks.append(current.decode("utf-8", "replace").strip())
    return [c for c in chunks if c]


class WeComBot:
    """单个「智能机器人」的长连接通道（一个实例 = 一个配置）。"""

    def __init__(self, bot_id: str, bot_secret: str,
                 default_chat_id: Optional[str] = None,
                 on_message: Optional[Callable[[str, Optional[List[str]]], None]] = None):
        self.bot_id = (bot_id or "").strip()
        self.bot_secret = (bot_secret or "").strip()
        self.default_chat_id = (default_chat_id or "").strip() or None
        self._on_message = on_message

        self._ready = self._authenticated = False
        self._ws = None
        self._ws_thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._auth_event = threading.Event()
        self._send_lock = threading.Lock()
        self._acks_lock = threading.Lock()
        self._pending_acks: Dict[str, dict] = {}
        self._known_targets: set = set()
        self._attempt = 0

        if not (self.bot_id and self.bot_secret):
            log.error("企业微信智能机器人配置不完整（bot_id / bot_secret）")
            return
        self._ready = True
        self._start()

    # ------------------------------------------------------------------ 生命周期
    def _start(self) -> None:
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop_event.clear()
        self._ws_thread = threading.Thread(target=self._run, name="wecom-bot-ws", daemon=True)
        self._ws_thread.start()
        self._hb_thread = threading.Thread(target=self._heartbeat, name="wecom-bot-hb", daemon=True)
        self._hb_thread.start()

    def stop(self) -> bool:
        self._stop_event.set()
        self._auth_event.clear()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        for th in (self._ws_thread, self._hb_thread):
            if th and th.is_alive() and th is not threading.current_thread():
                th.join(timeout=3)
        return not any(th and th.is_alive() for th in (self._ws_thread, self._hb_thread))

    def get_state(self) -> bool:
        return self._ready and self._auth_event.is_set()

    # ------------------------------------------------------------------ 连接主循环
    def _run(self) -> None:
        import websocket  # websocket-client

        while not self._stop_event.is_set():
            self._auth_event.clear()
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_msg,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(skip_utf8_validation=True)
            except Exception as err:
                log.error("企业微信智能机器人连接异常：%s", err)

            if self._stop_event.is_set():
                break
            delay = RECONNECT_DELAYS[min(self._attempt, len(RECONNECT_DELAYS) - 1)]
            self._attempt += 1
            log.info("企业微信智能机器人 %ss 后重连（第 %d 次）", delay, self._attempt)
            for _ in range(delay * 10):
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)
        self._attempt = 0

    def _heartbeat(self) -> None:
        while not self._stop_event.is_set():
            if self._auth_event.is_set():
                try:
                    self._send_raw({"cmd": "ping", "headers": {"req_id": _req_id("ping")}})
                except Exception:
                    pass
            for _ in range(HEARTBEAT_INTERVAL * 10):
                if self._stop_event.is_set():
                    return
                time.sleep(0.1)

    # ------------------------------------------------------------------ 回调
    def _on_open(self, ws) -> None:
        log.info("企业微信智能机器人已连接，开始订阅")
        self._send_raw({"cmd": "aibot_subscribe",
                        "headers": {"req_id": _req_id("aibot_subscribe")},
                        "body": {"bot_id": self.bot_id, "secret": self.bot_secret}})

    def _on_msg(self, ws, message: str) -> None:
        try:
            payload = json.loads(message)
        except Exception as err:
            log.error("解析企业微信智能机器人消息失败：%s", err)
            return

        req_id = (payload.get("headers") or {}).get("req_id")
        if req_id:
            self._resolve_ack(req_id, payload)

        cmd = payload.get("cmd")
        if not cmd:
            # 订阅回包：无 cmd，靠 req_id 前缀判定
            if str(req_id).startswith("aibot_subscribe"):
                if payload.get("errcode") == 0:
                    self._auth_event.set()
                    self._attempt = 0
                    log.info("企业微信智能机器人订阅成功")
                else:
                    log.error("企业微信智能机器人订阅失败：%s (%s)",
                              payload.get("errmsg"), payload.get("errcode"))
            return

        if cmd == "aibot_msg_callback":
            self._handle_message(payload)

    def _on_error(self, ws, error) -> None:
        self._auth_event.clear()
        log.error("企业微信智能机器人 WebSocket 错误：%s", error)

    def _on_close(self, ws, code, msg) -> None:
        self._auth_event.clear()
        log.info("企业微信智能机器人连接关闭：%s %s", code, msg)

    def _resolve_ack(self, req_id: str, payload: dict) -> None:
        with self._acks_lock:
            p = self._pending_acks.get(req_id)
        if not p:
            return
        p["payload"] = payload
        p["event"].set()

    # ------------------------------------------------------------------ 收发
    def _send_raw(self, payload: dict) -> None:
        ws = self._ws
        if not ws:
            raise RuntimeError("企业微信智能机器人未连接")
        ws.send(json.dumps(payload, ensure_ascii=False))

    def _send_with_ack(self, payload: dict) -> bool:
        req_id = (payload.get("headers") or {}).get("req_id")
        if not req_id:
            return False
        if not self._auth_event.wait(timeout=ACK_TIMEOUT):
            log.error("企业微信智能机器人未完成认证，无法发送")
            return False
        pending = {"event": threading.Event(), "payload": None}
        with self._acks_lock:
            self._pending_acks[req_id] = pending
        try:
            with self._send_lock:
                self._send_raw(payload)
            if not pending["event"].wait(timeout=ACK_TIMEOUT):
                log.error("企业微信智能机器人发送超时：req_id=%s", req_id)
                return False
            ack = pending["payload"] or {}
            if ack.get("errcode") != 0:
                log.error("企业微信智能机器人发送失败：%s (%s)",
                          ack.get("errmsg"), ack.get("errcode"))
                return False
            return True
        finally:
            with self._acks_lock:
                self._pending_acks.pop(req_id, None)

    def _handle_message(self, payload: dict) -> None:
        body = payload.get("body") or {}
        sender = ((body.get("from") or {}).get("userid") or "").strip()
        if not sender:
            return
        # 与 MP 一致：只处理单聊（groupPolicy=disabled）
        if body.get("chattype") == "group":
            log.debug("忽略企业微信智能机器人群聊消息")
            return

        text = self._extract_text(body)
        if not text:
            return
        self._remember_target(sender)
        log.info("收到企业微信智能机器人消息：userid=%s, text=%s", sender, text[:80])
        if self._on_message:
            try:
                self._on_message(sender, text)
            except Exception as err:
                log.error("处理企业微信智能机器人消息异常：%s", err)

    @staticmethod
    def _extract_text(body: dict) -> Optional[str]:
        """提取纯文本（text / voice / mixed；quote 兜底）。"""
        msgtype = body.get("msgtype")
        parts: List[str] = []
        if msgtype == "text":
            t = ((body.get("text") or {}).get("content") or "").strip()
            if t:
                parts.append(t)
        elif msgtype == "voice":
            t = ((body.get("voice") or {}).get("content") or "").strip()
            if t:
                parts.append(t)
        elif msgtype == "mixed":
            for item in (body.get("mixed") or {}).get("msg_item") or []:
                if item.get("msgtype") == "text":
                    c = ((item.get("text") or {}).get("content") or "").strip()
                    if c:
                        parts.append(c)
        quote = body.get("quote") or {}
        if not parts and quote.get("msgtype") == "text":
            qt = ((quote.get("text") or {}).get("content") or "").strip()
            if qt:
                parts.append(qt)
        text = "\n".join(p for p in parts if p).strip()
        if text:
            import re as _re
            text = _re.sub(r"@\S+", "", text).strip()
        return text or None

    # ------------------------------------------------------------------ 发送
    @staticmethod
    def _normalize_target(userid: Optional[str], default: Optional[str]) -> Tuple[Optional[str], int]:
        target = str(userid).strip() if userid else (default.strip() if default else None)
        if not target:
            return None, 1
        low = target.lower()
        if low.startswith("group:"):
            return target[6:].strip(), 2
        if low.startswith("user:"):
            return target[5:].strip(), 1
        return target, 1

    def _resolve_targets(self, userid: Optional[str] = None) -> List[Tuple[str, int]]:
        target, chat_type = self._normalize_target(userid, self.default_chat_id)
        if target:
            return [(target, chat_type)]
        return [(u, 1) for u in sorted(self._known_targets)]

    @staticmethod
    def _build_markdown(title: Optional[str] = None, text: Optional[str] = None) -> str:
        parts = []
        if title:
            parts.append("**%s**" % title)
        if text:
            parts.append(str(text).replace("\n\n", "\n"))
        return "\n\n".join(p for p in parts if p).strip()

    def send_markdown(self, content: str, userid: Optional[str] = None) -> bool:
        if not content:
            return False
        targets = self._resolve_targets(userid)
        if not targets:
            log.warning("未配置默认发送目标，且暂无已互动用户")
            return False
        ok_any = False
        for target, chat_type in targets:
            ok = True
            for chunk in _split_content(content):
                payload = {"cmd": "aibot_send_msg",
                           "headers": {"req_id": _req_id("aibot_send_msg")},
                           "body": {"chatid": target, "chat_type": chat_type,
                                    "msgtype": "markdown",
                                    "markdown": {"content": chunk}}}
                if not self._send_with_ack(payload):
                    ok = False
                    log.warning("向 %s 发送通知失败", target)
                    break
            ok_any = ok_any or ok
        return ok_any

    def send_msg(self, title: Optional[str] = None, text: Optional[str] = None,
                 userid: Optional[str] = None) -> bool:
        return self.send_markdown(self._build_markdown(title, text), userid=userid)

    def _remember_target(self, userid: str) -> None:
        if userid and userid not in self._known_targets:
            self._known_targets.add(userid)


# ---------------------------------------------------------------------------
# 进程级单例（notify 模块持有）
# ---------------------------------------------------------------------------

_BOT: Optional[WeComBot] = None
_BOT_LOCK = threading.Lock()


def start_bot(cfg: Dict[str, Any],
              on_message: Optional[Callable[[str, Optional[List[str]]], None]] = None) -> Optional[WeComBot]:
    """按配置启动（或重启）长连接；未配置 bot 凭据则返回 None。幂等。"""
    global _BOT
    bot_id = str((cfg or {}).get("bot_id") or "").strip()
    bot_secret = str((cfg or {}).get("bot_secret") or "").strip()
    chat_id = str((cfg or {}).get("bot_chat_id") or "").strip() or None
    with _BOT_LOCK:
        if not (bot_id and bot_secret):
            if _BOT is not None:
                _BOT.stop()
                _BOT = None
            return None
        if _BOT is not None and _BOT.bot_id == bot_id and _BOT.bot_secret == bot_secret:
            return _BOT
        if _BOT is not None:
            _BOT.stop()
        _BOT = WeComBot(bot_id, bot_secret, chat_id, on_message)
        return _BOT


def current_bot() -> Optional[WeComBot]:
    return _BOT


def stop_bot() -> None:
    global _BOT
    with _BOT_LOCK:
        if _BOT is not None:
            _BOT.stop()
            _BOT = None
