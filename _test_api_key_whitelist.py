#!/usr/bin/env python3
"""API 密钥级「调用量 + 白名单」归属回归测试。

背景（用户 2026-10-07 反馈）：

> 设置页面的 API 的调用量和白名单，应该对应每个单独的 API 而不是全部，
> 意思就是每个单独的 API 后面有调用量和白名单才对，主次关系混乱

原先只有**一份全局白名单**，且 UI 上摆在密钥列表**前面**，
于是看起来像「整个网关的开关」；而调用量虽然每行都有，头部却还挂着一个
全局「累计调用 N 次」汇总，同样让人分不清归属。

本测试锁定四条契约：

1. 每个密钥自带 whitelist 字段（新建即有，默认空 = 不限制来源）；
2. 单个密钥的白名单可独立设置，且**不影响其他密钥**；
3. 密钥级白名单真的参与鉴权：来源不在名单内 → 拒绝（mode=ip_denied）；
4. 全局白名单（免密钥）与密钥级白名单是两回事，互不覆盖。
"""
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_ok = 0
_fail: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra else ""))


tmpdir = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmpdir

api_keys = importlib.import_module("gateway.api_keys")
importlib.reload(api_keys)
api_keys.KEYS_FILE = Path(tmpdir) / "api_keys.json"

print("[1] 每个密钥自带 whitelist（新建即有，默认不限制来源）")

rec, err = api_keys.create_key("主密钥")
check("新建成功", rec is not None and err is None, f"rec={rec} err={err}")
check("密钥自带 whitelist 字段", "whitelist" in rec, f"got {list(rec or {})}")
check("默认为空（= 不限制来源）", rec.get("whitelist") == [], f"got {rec.get('whitelist')}")
check("调用量字段也存在", "callCount" in rec, f"got {list(rec or {})}")

print("\n[2] 单个密钥的白名单可独立设置，不影响其他密钥")

rec2, _ = api_keys.create_key("另一个密钥")
r, e = api_keys.set_key_whitelist(rec["id"], "192.168.31.5\n192.168.31.0/24")
check("设置成功", r is not None and e is None, f"r={r} e={e}")
check("解析出两条", r.get("whitelist") == ["192.168.31.5", "192.168.31.0/24"],
      f'got {r.get("whitelist")}')

# 关键：另一个密钥不受影响
cfg = api_keys.get_config()
k2 = [k for k in cfg["keys"] if k["id"] == rec2["id"]][0]
check("另一个密钥的白名单未被污染", k2.get("whitelist") == [], f"got {k2.get('whitelist')}")

# 逗号分隔也应支持（与全局白名单同一套解析）
r3, _ = api_keys.set_key_whitelist(rec["id"], "10.0.0.1,10.0.0.2")
check("逗号分隔同样支持", r3.get("whitelist") == ["10.0.0.1", "10.0.0.2"],
      f'got {r3.get("whitelist")}')

# 去重
r4, _ = api_keys.set_key_whitelist(rec["id"], "1.2.3.4\n1.2.3.4")
check("重复条目被去重", r4.get("whitelist") == ["1.2.3.4"], f'got {r4.get("whitelist")}')

# 不存在的 key
r5, e5 = api_keys.set_key_whitelist("nope", "1.1.1.1")
check("不存在的密钥返回错误而非崩溃", r5 is None and e5 is not None, f"r={r5} e={e5}")

print("\n[3] 密钥级白名单真的参与鉴权")

api_keys.set_require_key(True)
api_keys.set_key_whitelist(rec["id"], ["192.168.31.5"])
token = rec["key"]

# 来源不在名单内 → 拒绝
denied = api_keys.authenticate(token, client_host="203.0.113.9")
check("来源不在白名单内被拒", denied.get("ok") is False, f"got {denied}")
check("拒绝模式为 ip_denied", denied.get("mode") == "ip_denied", f"got {denied.get('mode')}")

# 来源在名单内 → 放行
allowed = api_keys.authenticate(token, client_host="192.168.31.5")
check("来源在白名单内放行", allowed.get("ok") is True, f"got {allowed}")

# 未配白名单的密钥不限制来源
open_ok = api_keys.authenticate(rec2["key"], client_host="203.0.113.9")
check("未配白名单的密钥不限制来源", open_ok.get("ok") is True, f"got {open_ok}")

# 不传 client_host 时跳过该检查（保持旧行为，内部调用不受影响）
legacy = api_keys.authenticate(token)
check("不传来源 IP 时跳过密钥级校验（兼容旧行为）", legacy.get("ok") is True, f"got {legacy}")

print("\n[4] 全局白名单与密钥级白名单是两回事")

api_keys.set_whitelist(["198.51.100.0/24"])
cfg = api_keys.get_config()
check("全局白名单独立保存", cfg.get("whitelist") == ["198.51.100.0/24"],
      f'got {cfg.get("whitelist")}')
k1 = [k for k in cfg["keys"] if k["id"] == rec["id"]][0]
check("密钥级白名单未被全局覆盖", k1.get("whitelist") == ["192.168.31.5"],
      f'got {k1.get("whitelist")}')
check("两者同时存在且各自独立",
      cfg.get("whitelist") != k1.get("whitelist"))

# 持久化后再载入，密钥级白名单仍在
importlib.reload(api_keys)
api_keys.KEYS_FILE = Path(tmpdir) / "api_keys.json"
api_keys._load_locked()
cfg2 = api_keys.get_config()
k1b = [k for k in cfg2["keys"] if k["id"] == rec["id"]][0]
check("重新载入后密钥级白名单仍在", k1b.get("whitelist") == ["192.168.31.5"],
      f'got {k1b.get("whitelist")}')
check("重新载入后全局白名单仍在", cfg2.get("whitelist") == ["198.51.100.0/24"],
      f'got {cfg2.get("whitelist")}')

shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
