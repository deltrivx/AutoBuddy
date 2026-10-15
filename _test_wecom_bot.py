#!/usr/bin/env python3
"""企微智能机器人 WebSocket 通道回归（2026-10-15）。

### 用户反馈

> 「我让你参考 MP 智能助手修复你自己，你跑去动 MP 智能助手？」

此前 AutoBuddy 的企微通知走 HTTP `cgi-bin/message/send`，要求出口 IP 在
企微后台「可信 IP」名单内。实测容器出口是 NAT 池
（112.194.50.154 / .151 / .1 轮换），三个候选出口全部 `errcode=60020`
⇒ 日报、任务通知、助手延后答复**全部**发不出去，
用户侧表现为「助手只回『正在查询』然后没下文」。

### 参考 MoviePilot 的解法

MP 的 `app/modules/wechat/wechatbot.py` 用**智能机器人 WebSocket 长连接**
（`wss://openws.work.weixin.qq.com`）：出站连接**不校验来源 IP**，
这正是「MP影视助手」能正常收发消息的原因。

协议（对齐 MP 实测）：
  - `aibot_subscribe` 订阅（body: bot_id / secret）
  - `aibot_msg_callback` 收消息（body.from.userid / text / msgtype）
  - `aibot_send_msg` 发消息（chatid / chat_type / msgtype=markdown）
  - `ping` 心跳 30s；`req_id` 配对 ACK，超时 10s 判失败
  - 单条按 4000 字节分片；重连退避 [1,2,5,10,30,60]

### 本实现

新增 `gateway/wecom_bot.py`，并接入 `notify` 的 wecom 渠道
（bot 优先 / HTTP 兜底）+ web_proxy 启动钩子 + Dockerfile 加
`websocket-client` 依赖。

本测试锁定上述契约。
"""

import ast
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
SRC_BOT = (ROOT / "gateway" / "wecom_bot.py").read_text(encoding="utf-8")
SRC_NT = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")
SRC_WP = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")
SRC_DF = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

_ok = 0
_fail = []


def check(name, cond, extra=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra else ""))


print("[1] wecom_bot.py 模块契约")

check("WS_URL 指向企微长连接网关", 'WS_URL = "wss://openws.work.weixin.qq.com"' in SRC_BOT)
check("aibot_subscribe 订阅", '"cmd": "aibot_subscribe"' in SRC_BOT)
check("aibot_msg_callback 收消息", '"aibot_msg_callback"' in SRC_BOT)
check("aibot_send_msg 发消息", '"cmd": "aibot_send_msg"' in SRC_BOT)
check("markdown 消息类型", '"msgtype": "markdown"' in SRC_BOT)
check("心跳 ping", '"cmd": "ping"' in SRC_BOT)
check("心跳间隔 30s", "HEARTBEAT_INTERVAL = 30" in SRC_BOT)
check("ACK 超时 10s", "ACK_TIMEOUT = 10" in SRC_BOT)
check("分片上限 4000 字节", "CHUNK_BYTES = 4000" in SRC_BOT)
check("重连退避表", "RECONNECT_DELAYS = [1, 2, 5, 10, 30, 60]" in SRC_BOT)
check("req_id 生成", "def _req_id(" in SRC_BOT)
check("ACK 配对机制", "_pending_acks" in SRC_BOT and "_resolve_ack" in SRC_BOT)
check("多字节安全分片（UTF-8 续字节判断）",
      "(encoded[end] & 0xC0) == 0x80" in SRC_BOT)
check("忽略群聊（与 MP 的 groupPolicy=disabled 一致）",
      'body.get("chattype") == "group"' in SRC_BOT)
check("voice/mixed 文本提取", 'msgtype == "voice"' in SRC_BOT and 'msgtype == "mixed"' in SRC_BOT)
check("quote 兜底提取", 'quote.get("msgtype") == "text"' in SRC_BOT)
check("去掉 @提及", 're.sub(r"@\\S+", "", text)' in SRC_BOT)
check("group:/user: 前缀归一", 'low.startswith("group:")' in SRC_BOT and 'low.startswith("user:")' in SRC_BOT)
check("配置不完整时不启动（只记错误）", "智能机器人配置不完整" in SRC_BOT)
check("start_bot 幂等（同凭据复用实例）",
      "if _BOT is not None and _BOT.bot_id == bot_id" in SRC_BOT)
check("stop_bot 存在", "def stop_bot(" in SRC_BOT)

print("\n[2] notify.py 渠道接入")

check("CHANNELS 增加 bot_id 字段", '"key": "bot_id"' in SRC_NT)
check("CHANNELS 增加 bot_secret（secret=True）", '"key": "bot_secret"' in SRC_NT)
check("CHANNELS 增加 bot_chat_id", '"key": "bot_chat_id"' in SRC_NT)
check("bot_secret 标记为 secret", '"key": "bot_secret", "label": "机器人 Secret", "secret": True' in SRC_NT)
check("导入 wecom_bot 模块", "import wecom_bot as _wcbot" in SRC_NT)
check("_send_wecom 优先走 WebSocket 通道", "bot.send_msg(text=text, userid=touser)" in SRC_NT)
check("bot 未就绪时回落 HTTP 通道", "corpid = str(ch_cfg.get(\"corpid\") or \"\").strip()" in SRC_NT)
check("public_view ready 判定纳入 bot 通道",
      "智能机器人（WebSocket）就绪也算" in SRC_NT)
check("start_wecom_bot 存在", "def start_wecom_bot(" in SRC_NT)
check("stop_wecom_bot 存在", "def stop_wecom_bot(" in SRC_NT)
check("bot 消息走统一命令路由", "wecom_command_reply(text, session_id=sender)" in SRC_NT)
check("start_wecom_bot 要求 bot_id + bot_secret 齐备",
      'str(cfg.get("bot_id") or "").strip()' in SRC_NT
      and 'str(cfg.get("bot_secret") or "").strip()' in SRC_NT)

print("\n[3] web_proxy / Dockerfile 接线")

check("web_proxy 启动即恢复长连接", "_notify.start_wecom_bot()" in SRC_WP)
check("Dockerfile 增加 websocket-client", "websocket-client" in SRC_DF)

print("\n[4] 行为验证（真实模块，不联网）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
sys.path.insert(0, str(ROOT))
from gateway import wecom_bot as wb  # noqa: E402

# 4a. 分片：短消息不拆
assert wb._split_content("短消息") == ["短消息"]
# 4b. 分片：超长按字节拆且不切断多字节字符
long_text = "中" * 3000                     # 9000 字节
chunks = wb._split_content(long_text)
assert len(chunks) >= 2, "超长消息应分片"
for ch in chunks:
    assert len(ch.encode("utf-8")) <= wb.CHUNK_BYTES, "分片超限"
    ch.encode("utf-8").decode("utf-8")      # 可往返 = 没切断字符
check("超长中文消息分片且不切断字符", True)
check("空内容返回空列表", wb._split_content("") == [])
# 4c. markdown 拼接
md = wb.WeComBot._build_markdown("标题", "正文")
assert md == "**标题**\n\n正文", repr(md)
md2 = wb.WeComBot._build_markdown(None, "只有正文")
assert md2 == "只有正文", repr(md2)
check("markdown 拼接正确（真换行）", True)
check("markdown 仅标题也正确", wb.WeComBot._build_markdown("T", None) == "**T**")
# 4d. target 归一
tn = wb.WeComBot._normalize_target
assert tn("user1", None) == ("user1", 1)
assert tn("group:g1", None) == ("g1", 2)
assert tn("user:u1", None) == ("u1", 1)
assert tn(None, "def") == ("def", 1)
assert tn(None, None) == (None, 1)
check("chatid 前缀归一（group:/user:）", True)
# 4e. 配置不完整时不启动
bot = wb.start_bot({"bot_id": "", "bot_secret": ""})
check("凭据为空时不启动", bot is None)
check("current_bot 为 None", wb.current_bot() is None)
# 4f. 分片边界：恰好等于上限
exact = "a" * wb.CHUNK_BYTES
assert wb._split_content(exact) == [exact]
check("恰好等于上限不拆片", True)

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
