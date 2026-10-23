#!/usr/bin/env python3
"""企微自定义菜单回归测试（MoviePilot create_menus 同款能力）。

用户要求：参考 MoviePilot 实现内置交互 —— 关键是**自动生成应用底部菜单**，
成员点按钮即交互，而不是让用户手打命令。本测试锁定该契约。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


print("[1] 菜单命令表")
check("命令表非空", len(notify.WECOM_MENU_COMMANDS) > 0)
check("每个菜单 key 都有命令映射",
      all(k in notify.MENU_KEY_TO_CMD for k, _n, _c in notify.WECOM_MENU_COMMANDS),
      str([k for k, _n, _c in notify.WECOM_MENU_COMMANDS if k not in notify.MENU_KEY_TO_CMD]))

print("\n[2] 菜单报文受企微协议上限约束（一级 3 / 二级 5）")
spec = notify.wecom_menu_spec()
buttons = spec["button"]
check("一级菜单 <= 3", len(buttons) <= 3, f"got {len(buttons)}")
for b in buttons:
    sub = b.get("sub_button")
    if sub:
        check("二级菜单 <= 5（%s）" % b.get("name"), len(sub) <= 5, f"got {len(sub)}")
check("按钮均为 click 或带 sub_button",
      all(b.get("type") == "click" or b.get("sub_button") for b in buttons))
# 所有按钮（含二级）都要有 key，否则点击无响应
all_items = []
for b in buttons:
    if b.get("sub_button"):
        all_items.extend(b["sub_button"])
    else:
        all_items.append(b)
check("所有按钮都有 key", all(i.get("key") for i in all_items))
check("所有 key 都能映射成命令",
      all(i["key"] in notify.MENU_KEY_TO_CMD for i in all_items))

print("\n[3] 菜单点击 -> 命令路由（event + EventKey，无 Content）")
for key, expect in [("AB_STATUS", "status"), ("AB_CREDITS", "credits"),
                    ("AB_HELP", "help"), ("AB_NOTIFY_ON", "notify on")]:
    got = notify.MENU_KEY_TO_CMD.get(key)
    check("%s -> %s" % (key, expect), got == expect, f"got {got}")

print("\n[4] 配置缺失时的健壮性（不得抛异常）")
r = notify.create_wecom_menus({})
check("空配置创建菜单返回失败而非异常", r.get("ok") is False)
r2 = notify.delete_wecom_menus({})
check("空配置删除菜单返回失败而非异常", r2.get("ok") is False)

print("\n[5] 源码契约（对齐 MoviePilot）")
SRC = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")
check("menu/create 端点", "/cgi-bin/menu/create" in SRC)
check("menu/delete 端点", "/cgi-bin/menu/delete" in SRC)
check("web_proxy 有菜单端点", "/api/notify/menu" in (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8"))

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
