#!/usr/bin/env python3
"""Telegram 通道回归测试（2026-10-13 新增渠道）。

契约：
1. 注册表含 telegram（bot_token / chat_id / proxy）；
2. **渠道级代理**：仅作用于本通道，不写全局、不影响网关 —— 与 MoviePilot 的
   全局 PROXY 不同（那里全局一份，这里每渠道独立）。代理只传给该请求；
3. bot_token 是 secret，GET 只回 has_value、明文不泄漏；
4. 配置缺失时的健壮性（不抛异常，返回友好错误）；
5. getMe 自检存在（不发消息即可验 Token）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
os.environ["AB_DATA_DIR"] = tempfile.mkdtemp(prefix="ab_tg_test_")
sys.path.insert(0, str(ROOT / "gateway"))

import notify  # noqa: E402

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


print("[1] 注册表")
tg = next((c for c in notify.CHANNELS if c["key"] == "telegram"), None)
check("含 telegram 渠道", tg is not None)
fields = {f["key"]: f for f in (tg["fields"] if tg else [])}
check("字段含 bot_token/chat_id/proxy",
      {"bot_token", "chat_id", "proxy"}.issubset(set(fields)),
      str(list(fields)))
check("bot_token 标记为 secret", bool(fields["bot_token"].get("secret")))
check("chat_id 与 proxy 非 secret",
      not fields["chat_id"].get("secret") and not fields["proxy"].get("secret"))
check("发送器已注册", "telegram" in notify._SENDERS)

print("\n[2] 配置保存与脱敏")
notify.update_config({"channels": {"telegram": {
    "bot_token": "***", "chat_id": "8646289271", "proxy": "http://192.168.31.10:7890"}}})
view = notify.public_view()["channels"]["telegram"]
check("bot_token 只回 has_value", view.get("bot_token") == {"has_value": True},
      str(view.get("bot_token")))
check("明文 token 不出现在响应", "123456789" not in json.dumps(view))
check("chat_id 明文回读", view.get("chat_id") == "8646289271")
check("proxy 明文回读", view.get("proxy") == "http://192.168.31.10:7890")

print("\n[3] 渠道级代理的隔离性（源码契约）")
SRC = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")
check("代理从本渠道配置读取", 'ch_cfg.get("proxy")' in SRC)
check("代理只传给该请求（kwargs 局部）", 'kwargs["proxy"] = proxy' in SRC)
check("未写全局环境变量", "os.environ[" not in SRC.split("def _send_telegram")[1][:900])

print("\n[4] 健壮性（配置缺失不得抛异常）")
r = notify._send_telegram({}, "x")
check("空配置发送返回失败", r.get("ok") is False and "不完整" in r.get("error", ""),
      str(r))
r2 = notify.telegram_get_me({})
check("空配置 getMe 返回失败", r2.get("ok") is False)
r3 = notify.send_test("telegram", "x")
check("send_test 不要求总开关", isinstance(r3, dict) and "ok" in r3)

print("\n[5] Telegram 不影响既有企微")
check("wecom 仍在注册表",
      any(c["key"] == "wecom" for c in notify.CHANNELS))
check("wecom 发送器仍在", "wecom" in notify._SENDERS)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
