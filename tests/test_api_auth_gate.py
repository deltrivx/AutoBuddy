# v1.0.8 安全修复回归：管理端点登录鉴权门
# 背景：2026-10-20 线上发现 21 条注入测试账号（pwn-* / cb-test / 路径穿越 /
# URL 编码变体）——根因是 /api/* 转发端点（import/delete/refresh-token 等）
# 全部无鉴权，未登录请求可直接写入。此测试锁定修复不回退。

import re
from pathlib import Path

print("[1] require_user 鉴权门已定义")
_ok = 0
_fail = []
def check(name, cond, hint=""):
    global _ok
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + hint))
    if cond: _ok += 1
    else: _fail.append(name)

PROXY = open(Path(__file__).resolve().parent.parent / "gateway/web_proxy.py", encoding="utf-8").read()

check("require_user 依赖函数存在",
      "def require_user(" in PROXY and "Depends(get_current_user)" in PROXY)
check("未登录返回 401",
      re.search(r"def require_user[\s\S]{0,400}401", PROXY) is not None)

print("\n[2] 管理端点全部挂鉴权")
route_re = re.compile(r'@app\.(post|get|put|delete)\("([^"]+)"\)\n((?:async )?def \w+\([^\n]*\n)')
routes = [(m.group(1), m.group(2), m.group(0)) for m in route_re.finditer(PROXY)]
# 元素: (method, path, defline) —— defline 整行含 Depends(...)，鉴权判定不吞括号
AUTHED = lambda defline: "require_user" in defline or "get_current_user" in defline
PUBLIC = {
    ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/status"), ("GET", "/api/auth/avatar"),
    ("GET", "/favicon.ico"),
    ("GET", "/api/notify/wecom/callback/{path}"), ("POST", "/api/notify/wecom/callback/{path}"),
    ("GET", "/api/gateway-info"),
}
noauth = [(m.upper(), p) for m, p, defline in routes if not AUTHED(defline)]
check("无鉴权端点只剩白名单", set(noauth) <= PUBLIC and len(noauth) > 0,
      "剩余: %s" % noauth)
check("高危写端点已挂门（import/delete/refresh-token/toggle）",
      all(any(m.upper() == "POST" and p == path and AUTHED(defline)
              for m, p, defline in routes)
          for path in ("/api/import", "/api/delete", "/api/refresh-token", "/api/account-pool/toggle")))
check("api-keys 全套已挂门",
      all(AUTHED(defline) for m, p, defline in routes if p.startswith("/api/api-keys")))

print("\n" + "=" * 50)
print(f"通过 {_ok} 项，失败 {len(_fail)} 项" + ("：" + str(_fail) if _fail else " ✅"))
if _fail:
    raise SystemExit(1)
