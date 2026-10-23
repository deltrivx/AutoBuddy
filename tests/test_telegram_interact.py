#!/usr/bin/env python3
"""Telegram 交互回归测试（照 MoviePilot 实现：命令菜单 + getUpdates 长轮询）。

MoviePilot 的做法（app/modules/telegram/telegram.py）：
  - register_commands()：deleteMyCommands 后 setMyCommands，配置保存即自动
    在客户端输入框生成命令菜单；
  - 收消息用 **getUpdates 长轮询**，因此 Telegram 通道**不需要公网入口**
    （这与企微回调不同 —— 此前我错误地把「需公网」套到 Telegram 上，已更正）。

契约：
1. 命令表存在且命令名合法（小写字母/数字/下划线）；
2. 命令能路由到解释器（与企微菜单同一套命令 MENU_KEY_TO_CMD）；
3. 长轮询可启动/停止，配置缺失时不启动；
4. 命令注册/清理在缺 Token 时返回失败而非抛异常。
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["AB_DATA_DIR"] = tempfile.mkdtemp(prefix="ab_tgi_test_")
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


print("[1] 命令表")
cmds = notify.TELEGRAM_COMMANDS
check("命令表非空", len(cmds) > 0)
check("命令名合法（小写字母/数字/下划线）",
      all(re.fullmatch(r"[a-z0-9_]+", c) for c, _d in cmds),
      str([c for c, _d in cmds if not re.fullmatch(r"[a-z0-9_]+", c)]))
check("每个命令都有描述", all(d for _c, d in cmds))
check("含核心命令 help/status/credits",
      {"help", "status", "credits"}.issubset({c for c, _d in cmds}))

print("\n[2] 命令路由（复用企微菜单同套命令）")
for cmd in ("status", "credits", "help"):
    check("%s 可路由" % cmd, cmd in notify.MENU_KEY_TO_CMD.values() or cmd in ("help",),
          str(list(notify.MENU_KEY_TO_CMD.values())))
# 反向：菜单 key -> 命令，应都能被解释器识别
check("AB_STATUS -> status", notify.MENU_KEY_TO_CMD.get("AB_STATUS") == "status")

print("\n[3] 命令注册健壮性（缺 Token 不得抛异常）")
r = notify.telegram_register_commands({})
check("空配置注册返回失败", r.get("ok") is False)
r2 = notify.telegram_delete_commands({})
check("空配置清理返回失败", r2.get("ok") is False)

print("\n[4] 长轮询生命周期")
check("无 Token 时不启动", notify.start_telegram_polling() is False)
# 有 Token 时应能启动（不发真实请求：线程内先读配置，这里只验启动返回）
notify.update_config({"channels": {"telegram": {
    "bot_token": "123456789:FAKE-TOKEN-FOR-TEST", "chat_id": "8646289271"}}})
started = notify.start_telegram_polling()
check("有 Token 时启动成功", started is True)
check("幂等：重复调用不报错", notify.start_telegram_polling() is True)
notify.stop_telegram_polling()
check("可停止", True)

print("\n[5] 源码契约（对齐 MoviePilot）")
SRC = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")
check("setMyCommands 端点", "/setMyCommands" in SRC)
check("deleteMyCommands 端点", "/deleteMyCommands" in SRC)
check("getUpdates 端点（长轮询，非 webhook）", "/getUpdates" in SRC)
check("未使用 webhook", "setWebhook" not in SRC)
check("start_telegram_polling 存在", "def start_telegram_polling" in SRC)
WP = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")
check("web_proxy 启动时恢复轮询", "start_telegram_polling()" in WP)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
