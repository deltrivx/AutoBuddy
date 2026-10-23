#!/usr/bin/env python3
"""交互命令路由唯一性回归（2026-10-13 用户二次反馈「交互菜单还是没变」）。

### 实测根因

`gateway/web_proxy.py` 的企微回调处理器里**内联了一份** if/elif 命令链，
与 `gateway/notify.py` 的 `_wecom_interpret` 是两份代码。

改 notify.py 的口径完全不生效 —— 因为日志显示 POST /api/notify/wecom/callback
由 web_proxy（18090）服务，回调实际走 web_proxy 那份内联链。

症状：
  - 直接调 `notify._wecom_interpret("text", "accounts")` → 返回新口径 ✅
  - 走 HTTP 回调端点 → 返回旧口径（23 个账号逐条列表）❌
  两者不一致，用户看到的永远是旧的。

### 修复

命令语义收拢为**唯一实现** `notify.wecom_command_reply()`：
  - web_proxy 回调只做加解密 + `run_in_threadpool` 委托；
  - `_wecom_interpret` 也改为纯委托（MenuKey→文本→同一函数）；
  - 删除 web_proxy 里 ~5KB 的内联命令链。

口径（用户 2026-10-13 需求）：
  - 账号余额 → **完整列出**全部账号积分（此前只给顶部四项）；
  - 模型     → 「当前模型」+ 全量清单 + 轮检禁用/手动禁用分类。

本测试锁定：唯一实现存在、两份入口都委托、web_proxy 无内联链、
口径函数齐全。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_NT = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")
SRC_WP = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")

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


print("[1] 唯一命令实现")

check("wecom_command_reply 存在", "def wecom_command_reply(" in SRC_NT)
check("_cmd_accounts 存在", "def _cmd_accounts(" in SRC_NT)
check("_cmd_models 存在", "def _cmd_models(" in SRC_NT)
check("_cmd_credits 存在", "def _cmd_credits(" in SRC_NT)
check("_cmd_status 存在", "def _cmd_status(" in SRC_NT)
check("_cmd_daily / _cmd_run / _cmd_checkin 存在",
      all(f"def _cmd_{n}(" in SRC_NT for n in ("daily", "run", "checkin")))

print("\n[2] 两个入口都委托到唯一实现")

# web_proxy 回调：必须调用 wecom_command_reply，不能再有自己的命令分支
check("web_proxy 调用 wecom_command_reply",
      "wecom_command_reply" in SRC_WP)
check("web_proxy 不再有 accounts 分支", 'elif cmd == "accounts"' not in SRC_WP)
check("web_proxy 不再有 credits 分支", 'elif cmd == "credits"' not in SRC_WP)
check("web_proxy 不再有 models 分支", 'elif cmd == "models"' not in SRC_WP)
check("web_proxy 不再有 status/daily/run/checkin 分支",
      not any(f'elif cmd == "{c}"' in SRC_WP for c in ("status", "daily", "run", "checkin")))
check("MP 模式：回调立即返回 + 后台线程处理（不再阻塞事件循环）",
      "threading.Thread(" in SRC_WP and "_wecom_handle_async" in SRC_WP
      and "wecom_command_reply" in SRC_WP)

# _wecom_interpret 也改为纯委托
tree_nt = ast.parse(SRC_NT)
fn = next((n for n in tree_nt.body
           if isinstance(n, ast.FunctionDef) and n.name == "_wecom_interpret"), None)
check("_wecom_interpret 存在", fn is not None)
if fn is not None:
    calls = [n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    check("_wecom_interpret 委托 wecom_command_reply",
          "wecom_command_reply" in calls, f"calls={calls}")
    # 不再直接 urllib（命令语义已下沉）
    src_fn = ast.get_source_segment(SRC_NT, fn) or ""
    check("_wecom_interpret 不再自己发 HTTP",
          "urlopen" not in src_fn)

print("\n[3] 口径函数正确性（静态检查调用链）")

check("_cmd_accounts 打 /api/credits/stats",
      "/api/credits/stats" in SRC_NT)
check("_cmd_accounts 输出账号明细（非顶部四项）",
      "账号余额" in SRC_NT and "currentRemaining" in SRC_NT)
check("_cmd_models 打 /api/account-models 取全量",
      "/api/account-models" in SRC_NT)
check("_cmd_models 输出禁用分类",
      "轮检禁用" in SRC_NT and "手动禁用" in SRC_NT)
check("_cmd_models 输出全量清单",
      "模型清单" in SRC_NT)
check("_cmd_credits 输出顶部四项",
      all(k in SRC_NT for k in ("usageToday", "usage7Days", "usageThisMonth")))

print("\n[4] notify 内部 HTTP 基址常量")

check("_WEBUI_BASE 指向 18090", '"http://127.0.0.1:18090"' in SRC_NT)
check("_GW_BASE 指向 18091", '"http://127.0.0.1:18091"' in SRC_NT)
check("_http_json 助手存在", "def _http_json(" in SRC_NT)

print("\n[5] 真实模块导入 + 路由分发（不依赖网络）")

import sys
sys.path.insert(0, str(ROOT))
from gateway import notify  # noqa: E402,F401

check("notify.wecom_command_reply 可导入", callable(notify.wecom_command_reply))
# 不需要网络的分支：help / notify on-off / 未知命令
check("help 返回指令清单",
      "指令" in notify.wecom_command_reply("help"))
check("未知命令有兜底（不发请求）",
      "帮助" in notify.wecom_command_reply("随便说点啥xyz"))
check("英文 status 归一化到状态分支",
      notify.wecom_command_reply("/status").startswith("AutoBuddy")
      or "失败" in notify.wecom_command_reply("/status"))

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
