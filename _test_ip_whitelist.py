#!/usr/bin/env python3
"""IP 白名单免验证的单测。

覆盖三件事（错了都会静默放行/静默拦截，所以必须钉死）：
  1. _ip_in_whitelist 的匹配语义：精确 IP、CIDR、IPv4-mapped IPv6、非法条目
  2. 白名单只在 requireKey=True 时生效（关掉校验时白名单无意义）
  3. 白名单命中时免密钥、未命中时仍要密钥
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

# 用临时目录隔离，别碰真实数据
import tempfile
tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp

try:
    from gateway import api_keys
except ImportError:
    import api_keys

print("[1] _ip_in_whitelist 匹配语义")
wl = ["192.168.31.5", "10.0.0.0/8", "::ffff:172.16.1.9"]
check("精确 IP 命中", api_keys._ip_in_whitelist("192.168.31.5", wl))
check("精确 IP 未命中", not api_keys._ip_in_whitelist("192.168.31.6", wl))
check("CIDR 网段内命中", api_keys._ip_in_whitelist("10.20.30.40", wl))
check("CIDR 网段外未命中", not api_keys._ip_in_whitelist("11.0.0.1", wl))
check("IPv4-mapped 归一后命中", api_keys._ip_in_whitelist("::ffff:192.168.31.5", wl))
check("名单里的 mapped 形态也能匹配", api_keys._ip_in_whitelist("172.16.1.9", wl))
check("空 IP 不命中", not api_keys._ip_in_whitelist("", wl))
check("空名单不命中", not api_keys._ip_in_whitelist("192.168.31.5", []))
check("非法 IP 不抛异常", api_keys._ip_in_whitelist("not-an-ip", wl) is False)
check("名单含非法条目时跳过而非报错", api_keys._ip_in_whitelist("192.168.31.5", ["bad/entry", "192.168.31.5"]))

print()
print("[2] set_whitelist 归一化")
api_keys.set_whitelist("192.168.1.1, 192.168.1.2\n10.0.0.0/8")
cfg = api_keys.get_config()
check("逗号+换行都能拆", cfg["whitelist"] == ["192.168.1.1", "192.168.1.2", "10.0.0.0/8"])
check("count 正确", cfg["whitelistCount"] == 3)
api_keys.set_whitelist([" 1.2.3.4 ", "1.2.3.4", ""])
check("去重+去空+去空格", api_keys.get_config()["whitelist"] == ["1.2.3.4"])
api_keys.set_whitelist("")
check("清空后白名单为空", api_keys.get_config()["whitelist"] == [])

print()
print("[3] get_state 暴露 whitelist（网关鉴权靠它判断）")
api_keys.set_whitelist("192.168.31.5")
st = api_keys.get_state()
check("state 含 whitelist", st.get("whitelist") == ["192.168.31.5"])
check("state 含 requireKey", "requireKey" in st)

print()
print("[4] 持久化：白名单要能跨重启存活")
api_keys.set_whitelist("172.16.0.0/12")
api_keys._load_locked()
check("重新加载后仍在", api_keys.get_config()["whitelist"] == ["172.16.0.0/12"])

print()
print("=" * 50)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
