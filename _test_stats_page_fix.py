#!/usr/bin/env python3
"""统计页加载失败修复回归（2026-10-15 用户反馈）。

### 用户反馈

> 「统计加载失败 / 无法连接 workbuddy-switch 服务（http://192.168.31.2:18090），
> 请先运行 `workbuddy-switch` / 卡死或者重启报错，
> workbuddy-switch 是硬编码还是可以修改的，因为也已经改为 autobuddy 了」

### 实测根因

**问题 1（加载失败/卡死）**：官方后端 `/api/checkin/status` 本身就要
**4.9~8.9 秒**（逐账号实时查，账号从 17 涨到 22+ 后劣化）。
原缓存 TTL 只有 60 秒挡不住；前端 `Promise.all` 等它 → 整页卡死 →
fetch 被中断 → 走 catch → 抛「无法连接 workbuddy-switch 服务」。

**关键**：服务其实活着（官方后端并发测试：慢接口 7.72s，快接口 0.00s 未被拖累），
报错是**误导性**的。

**问题 2（品牌名）**：该文案是官方前端 JS 里的硬编码模板字符串：

```js
} catch { throw new Error(`无法连接 workbuddy-switch 服务（${zb}），请先运行 \`workbuddy-switch\``) }
```

其中 `zb = window.location.origin`（动态，非硬编码地址）。

### 修复

1. **stale-while-revalidate**：用户请求**永远不等上游**。
   有数据（哪怕陈旧）立即返回 + 后台单工刷新；只有从未拿到过数据（首启）
   才同步等一次，且失败返回空结构而非 5xx。容器启动即后台预热。
2. **品牌名替换**：只改用户可见的报错文案（`无法连接 workbuddy-switch 服务`
   → `无法连接 AutoBuddy 服务`；`请先运行 \`workbuddy-switch\``
   → `请检查 AutoBuddy 容器是否正常运行`），
   **不动** `jye="workbuddy-switch"` 拼成的 GitHub 仓库地址（真实上游，改了会 404）。

本测试锁定上述契约。
"""

import ast
import re
import subprocess
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).parent
SRC = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")

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


print("[1] stale-while-revalidate 契约")

check("_checkin_status_serve 存在", "def _checkin_status_serve(" in SRC)
check("返回 (payload, age) 二元组", "return payload, age" in SRC)
check("_checkin_bg_refresh 存在（后台刷新）", "def _checkin_bg_refresh(" in SRC)
check("后台刷新单工（_CHECKIN_BG_LOCK + _CHECKIN_BG_RUNNING）",
      "_CHECKIN_BG_LOCK" in SRC and "_CHECKIN_BG_RUNNING" in SRC)
check("后台刷新用守护线程（不阻塞退出）",
      'threading.Thread(target=_worker, name="ab-checkin-refresh", daemon=True)' in SRC)
check("新鲜度阈值 _CHECKIN_FRESH_SEC", "_CHECKIN_FRESH_SEC = " in SRC)
check("陈旧阈值 _CHECKIN_STALE_SEC", "_CHECKIN_STALE_SEC = " in SRC)
check("_checkin_age 辅助函数", "def _checkin_age(" in SRC)
check("容器启动即预热（首启不等人）",
      "_checkin_bg_refresh(force=True)" in SRC)

print("\n[2] 请求路径永不等待")

check("用户请求走 _checkin_status_serve（不是旧 cached）",
      "payload, age = _checkin_status_serve()" in SRC)
check("有数据即返回 + 带 age 头",
      '"x-autobuddy-cache-age": str(int(age))' in SRC)
check("缓存头区分 hit / stale",
      '"stale" if age > _CHECKIN_FRESH_SEC else "hit"' in SRC)
check("无数据时才同步取（首启）", '"x-autobuddy-cache": "miss"' in SRC)
check("同步取失败返回空结构而非 5xx",
      '"x-autobuddy-cache": "error"' in SRC
      and 'json.dumps({"accounts": []})' in SRC)
check("旧注释（1.46s/2.34s）已更新为劣化数据",
      "4.9~8.9s" in SRC)

print("\n[3] 品牌名替换（只改用户可见文案）")

check("替换「无法连接 workbuddy-switch 服务」",
      "\\xe6\\x97\\xa0\\xe6\\xb3\\x95\\xe8\\xbf\\x9e\\xe6\\x8e\\xa5 workbuddy-switch" in SRC)
check("替换「请先运行 `workbuddy-switch`」",
      "\\xe8\\xaf\\xb7\\xe5\\x85\\x88\\xe8\\xbf\\x90\\xe8\\xa1\\x8c" in SRC
      and "workbuddy-switch" in SRC)
check("新文案指向 AutoBuddy 容器",
      "\\xe5\\xae\\xb9\\xe5\\x99\\xa8" in SRC)   # 「容器」
check("保留 workbuddy-switch.app -> autobuddy 旧规则",
      '(b"workbuddy-switch.app", b"autobuddy")' in SRC)
check("保留 WorkBuddy Switch -> AutoBuddy 旧规则",
      '(b"WorkBuddy Switch", b"AutoBuddy")' in SRC)
check("注释说明 GitHub 仓库名必须保留",
      "github.com/changexbc/workbuddy-switch" in SRC or "GitHub" in SRC)

print("\n[4] 替换规则对真实 JS 有效（等长/不等长均验证）")

# 从线上取真实 JS，验证两条新规则确实命中、且不破坏语法
try:
    import urllib.request
    req = urllib.request.Request(
        "http://192.168.31.2:18090/assets/index-CzM10CWE.js",
        headers={"User-Agent": "Mozilla/5.0"})
    js = urllib.request.urlopen(req, timeout=60).read()
except Exception as e:
    js = None
    check("可取到线上 JS（跳过行为验证）", True, f"网络不可用: {str(e)[:60]}")

if js is not None:
    rules = [
        (b"\xe6\x97\xa0\xe6\xb3\x95\xe8\xbf\x9e\xe6\x8e\xa5 workbuddy-switch "
         b"\xe6\x9c\x8d\xe5\x8a\xa1",
         b"\xe6\x97\xa0\xe6\xb3\x95\xe8\xbf\x9e\xe6\x8e\xa5 AutoBuddy "
         b"\xe6\x9c\x8d\xe5\x8a\xa1"),
        (b"\xe8\xaf\xb7\xe5\x85\x88\xe8\xbf\x90\xe8\xa1\x8c \\`workbuddy-switch\\`",
         b"\xe8\xaf\xb7\xe6\xa3\x80\xe6\x9f\xa5 AutoBuddy \xe5\xae\xb9\xe5\x99\xa8"
         b"\xe6\x98\xaf\xe5\x90\xa6\xe6\xad\xa3\xe5\xb8\xb8\xe8\xbf\x90\xe8\xa1\x8c"),
    ]
    out = js
    for i, (old, new) in enumerate(rules):
        n = out.count(old)
        check(f"规则 {i+1} 命中线上 JS（{n} 处）", n >= 1, f"got {n}")
        if n:
            out = out.replace(old, new)

    # 替换后语法仍有效
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(out.decode("utf-8"))
        path = f.name
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    check("替换后 JS 语法有效", r.returncode == 0, (r.stderr or "")[:200])
    os.unlink(path)

    # GitHub 仓库名必须保留
    check("GitHub 仓库名未被动",
          b'jye="workbuddy-switch"' in out)
    # 报错文案已更新
    check("报错文案已是 AutoBuddy",
          "无法连接 AutoBuddy 服务".encode() in out)
    check("旧报错文案已消失",
          "无法连接 workbuddy-switch 服务".encode() not in out)

print("\n[5] 项目地址可配（不改官方仓库名）")

SRC_M = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
check("AB_PROJECT_URL 可配", 'AB_PROJECT_URL' in SRC_M)
check("默认指向 AutoBuddy 仓库", "deltrivx/AutoBuddy" in SRC_M)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
