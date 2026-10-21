#!/usr/bin/env python3
"""v1.0.18 安全修复回归：网关本体（main.py，18091）管理端点鉴权门。

## 为什么单独立一个测试文件

v1.0.8 修过一次同类问题，但那一次只覆盖了 **web_proxy（18090）** 的
``/api/*`` 转发端点（54 道 require_user 门，见 ``_test_api_auth_gate.py``）。
网关本体 ``main.py`` 一个都没碰 —— 而 18091 在 host 网络下是 0.0.0.0 监听，
局域网内任何主机都能直连。线上实测（2026-10-21）：

    $ curl -X POST http://192.168.31.2:18091/import -d '{"accounts":[...]}'
    {"ok":true,"added":1,...}          ← 未登录，直接写进账号池

第二次同类注入事件就是这么进来的。更糟的是 ``_save_accounts`` 不留任何备份，
所以「谁、什么时候、加了什么」在备份链里完全看不出痕迹。

本文件锁定三件事，防止回退：

1. **main.py 自己的路由**：除白名单外全部挂 require_user；
2. **白名单必须精确**：/health、企业微信回调、/v1/*（OpenAI 兼容 API）。
   多放一个进来就等于重新开一扇门，少放一个就会打断监控或下游客户端；
3. **_save_accounts 落盘前留底**，且只保留有限份数。

跑法：python3 _test_gateway_auth_gate.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent
MAIN = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
PROXY = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")

_ok = 0
_fail = []


def check(label, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _fail.append(label)
        print(f"  FAIL {label}  {detail}")


# ---------------------------------------------------------------------------
print("== 1. 鉴权门本体：与 web_proxy 同一套判定 ==")
# ---------------------------------------------------------------------------
check("main.py 定义了 require_user", "def require_user(" in MAIN)
check("require_user 依赖 get_current_user",
      re.search(r"def require_user\([^)]*Depends\(get_current_user\)", MAIN) is not None)
check("未登录抛 401",
      re.search(r"def require_user[\s\S]{0,400}401", MAIN) is not None)
check("get_current_user 读会话 Cookie",
      re.search(r"def get_current_user\([^)]*Cookie\(", MAIN) is not None)
check("会话判定复用 db.validate_session（不另写一套）",
      "validate_session" in MAIN and "from gateway import db as _db" in MAIN)

# ---------------------------------------------------------------------------
print("\n== 2. main.py 路由逐个核对：白名单外一律挂门 ==")
# ---------------------------------------------------------------------------
# 解析「装饰器 + def 签名」——签名整行含 Depends(...)，判定不吞括号。
# 注意堆叠装饰器：@app.get("/v1/models") 紧跟 @app.get("/models") 时，
# 两个路径共用同一个 def，必须把连续多行装饰器一起吃掉，
# 否则只会解析到第二个路径、漏掉第一个。
route_re = re.compile(
    r'((?:@app\.(?:get|post|put|delete|patch)\("[^"]+"\)\n)+)((?:async )?def \w+\([^\n]*\))')
routes = []
for m in route_re.finditer(MAIN):
    blob = m.group(0)
    for deco in re.finditer(r'@app\.(\w+)\("([^"]+)"\)', m.group(1)):
        routes.append((deco.group(1).upper(), deco.group(2), blob))
check("解析到 main.py 的路由（37 组，含 2 组堆叠别名）", len(routes) >= 37, f"got {len(routes)}")

AUTHED = lambda blob: "require_user" in blob
noauth = [(m, p) for m, p, blob in routes if not AUTHED(blob)]

# 刻意保留公开的白名单，逐条给理由（改这里必须同步改代码注释与发布说明）。
PUBLIC = {
    # 容器健康检查 / Unraid 探活 / 部署后 curl 验证 —— 加了门会把监控打成 401。
    ("GET", "/health"),
    # 企业微信服务器回调：外部固定入口，请求由平台发起，不可能带本站会话 Cookie；
    # 安全性来自 cb_token / cb_aes_key 签名校验。
    ("GET", "/api/notify/wecom/callback"),
    ("POST", "/api/notify/wecom/callback"),
    # OpenAI 兼容 API：鉴权走 api_keys（_gateway_auth），与会话门是两套体系。
    ("GET", "/v1/models"),
    ("GET", "/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/chat/completions"),
}
check("未挂门的端点恰好等于白名单", set(noauth) == PUBLIC, f"实际: {sorted(noauth)}")
check("白名单里没有高危写端点（import/delete/toggle/rotate/api-keys）",
      not any(p in {"/import", "/delete", "/account-pool/toggle", "/rotate/run",
                    "/api-keys", "/api-keys/delete", "/api-keys/delete-all",
                    "/api-keys/config", "/account-models/config",
                    "/account-pool/config", "/model-health/config",
                    "/account-pool/risk-flag/clear"}
              for _, p in noauth),
      f"白名单里混进了: {noauth}")

# ---------------------------------------------------------------------------
print("\n== 3. 第二次事件的入口必须已挂门（点名） ==")
# ---------------------------------------------------------------------------
by_path = {(m, p): blob for m, p, blob in routes}
for method, path in [
    ("POST", "/import"),          # 注入入口
    ("POST", "/delete"),          # 删号入口
    ("GET", "/export-accounts"),  # 凭据外泄入口
    ("POST", "/refresh-token"),
    ("GET", "/account-pool/status"),
    ("PUT", "/account-pool/config"),
    ("POST", "/account-pool/toggle"),
    ("POST", "/account-pool/risk-flag/clear"),
    ("POST", "/account-health/probe"),
    ("GET", "/account-health/audit"),
    ("GET", "/account-models/config"),
    ("PUT", "/account-models/config"),
    ("POST", "/account-models/restore"),
    ("GET", "/api-keys/status"),
    ("POST", "/api-keys"),
    ("POST", "/api-keys/update"),
    ("POST", "/api-keys/reset-stats"),
    ("POST", "/api-keys/delete"),
    ("POST", "/api-keys/delete-all"),
    ("PUT", "/api-keys/config"),
    ("GET", "/reqlog"),
    ("GET", "/rotate/status"),
    ("POST", "/rotate/run"),
    ("GET", "/model-health/config"),
    ("PUT", "/model-health/config"),
    ("GET", "/model-health/status"),
    ("POST", "/model-health/run"),
    ("GET", "/gateway/info"),
    ("GET", "/account-pool/selections"),
    ("POST", "/account-pool/selections/reset"),
]:
    check(f"{method} {path} 已挂门", AUTHED(by_path.get((method, path), "")),
          "未找到该路由或未挂门")

# ---------------------------------------------------------------------------
print("\n== 4. 公开端点不得被误挂门（否则监控/回调/下游客户端全断） ==")
# ---------------------------------------------------------------------------
for method, path in [
    ("GET", "/health"),
    ("GET", "/api/notify/wecom/callback"),
    ("POST", "/api/notify/wecom/callback"),
]:
    check(f"{method} {path} 保持公开", not AUTHED(by_path.get((method, path), "x")),
          "被误挂了门")
# /v1/* 的鉴权是 _gateway_auth（api_keys），不是 require_user
for method, path in [("GET", "/v1/models"), ("POST", "/v1/chat/completions")]:
    blob = by_path.get((method, path), "")
    check(f"{method} {path} 走 api_keys 而非会话门",
          not AUTHED(blob) and "_gateway_auth" in MAIN)

# ---------------------------------------------------------------------------
print("\n== 5. web_proxy 转发必须透传登录态（否则自家面板全部 401） ==")
# ---------------------------------------------------------------------------
check("定义了 _gw_headers 透传辅助函数", "def _gw_headers(" in PROXY)
check("会话 Cookie 名与 db 一致（autobuddy_session）",
      'GW_SESSION_COOKIE = "autobuddy_session"' in PROXY)
check("_forward_gateway 带上 request 以透传会话",
      re.search(r"async def _forward_gateway\([^)]*request:", PROXY) is not None)
check("_forward_gateway 内部用 _gw_headers 构造头",
      "_gw_headers(request" in PROXY)
check("转发点数量足够（_forward_gateway 调用 ≥ 10 处带 request）",
      PROXY.count("_forward_gateway(") >= 11 and PROXY.count(", request=request)") >= 10,
      f"forward 调用 {PROXY.count('_forward_gateway(')} 处")
# 关键：直接转发的写端点也必须带头
# 用更直接的判据：web_proxy 里 _gw_headers(request 出现次数应覆盖全部管理转发点
check("管理端点转发全部带上会话头（_gw_headers 调用次数 ≥ 19）",
      PROXY.count("_gw_headers(request") >= 19,
      f"got {PROXY.count('_gw_headers(request')}")
check("/health 与 /v1/models 的转发**不**带头（公开端点无需会话）",
      re.search(r'client\.get\(GATEWAY_BASE_URL \+ "/health"\)', PROXY) is not None)

# ---------------------------------------------------------------------------
print("\n== 6. _save_accounts 落盘前留底 ==")
# ---------------------------------------------------------------------------
check("_save_accounts 调用 _backup_accounts", "_backup_accounts(" in MAIN)
check("_backup_accounts 在写入之前调用（留底先于 payload 落盘）",
      MAIN.find("_backup_accounts(") < MAIN.find("os.replace(tmp, target)"))
check("★ 留底对象是实际写入目标（targets[0]），不是硬编码路径",
      "_backup_accounts(targets[0])" in MAIN,
      "硬编码 primary 会在权威源缺席时静默失败：『留底了但一个文件都没有』")
check("备份名带时间戳（可追溯是哪一次变更）",
      re.search(r'bak-\{stamp\}', MAIN) is not None or "bak-" in MAIN)
check("备份份数有上限（不无限堆积）", "_ACCOUNTS_BAK_KEEP" in MAIN)
check("有清理旧备份的实现", "def _prune_accounts_backups(" in MAIN)
check("★ 清理按 mtime 排序（同一秒的后缀会让文件名字典序错乱）",
      "st_mtime" in MAIN)
check("★ 同一秒内多次留底用递增后缀，不是 pid",
      "os.getpid()" not in MAIN.split("def _backup_accounts")[1].split("def _prune_accounts_backups")[0],
      "pid 在同进程内是定值，会让同秒第 2/3/4 次全覆盖同一个文件")
check("留底失败不打断写入（try/except 吞掉）",
      re.search(r"def _backup_accounts[\s\S]{0,1400}except Exception", MAIN) is not None)

# ---------------------------------------------------------------------------
print("\n== 7. 会话门不削弱 /v1 既有鉴权 ==")
# ---------------------------------------------------------------------------
check("_gateway_auth 仍在（api_keys 那套没被替换）", "def _gateway_auth(" in MAIN)
check("chat_completions 仍调 _gateway_auth",
      re.search(r'async def chat_completions[\s\S]{0,600}_gateway_auth', MAIN) is not None)

print("\n" + "=" * 56)
print(f"通过 {_ok} 项，失败 {len(_fail)} 项" + ("：" + str(_fail) if _fail else " ✅"))
if _fail:
    raise SystemExit(1)
