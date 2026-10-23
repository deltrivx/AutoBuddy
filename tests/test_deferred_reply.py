#!/usr/bin/env python3
"""LLM 延后推送 + 能力问句即时化（2026-10-14 用户反馈修复）。

### 用户反馈

> 「我刚刚发送了一个：你可以做什么，到现在都没回我……只是那个服务 MP
> 这个服务 AutoBuddy，不是让你自己从 0 开始写个半成品」

### 实测根因（不是 prompt 问题，是架构缺陷）

企微「接收消息服务器」要求 **5 秒内**返回响应体，超时即丢弃。
实测 LLM 链路耗时 **9.0 秒**：

```
Q: 你可以做什么
   (9.0s) 我可以帮你查询 AutoBuddy 网关的各类实时状态，主要有这些：...
   判定: ❌ 超时！企微会丢弃回复
```

服务侧其实算出来了，但企微把回复丢了 —— 用户侧表现为「没回」。

### 参考 MoviePilot 的解法

研究 `app/application/messaging/ingress.py` 后确认其分层：**渠道只负责接收与
即时回执，重活交给后台，结果走主动推送通道**。本次照此实现。

### 修复（三道防线）

1. **能力/菜单类问句本地即时生成**（`_looks_like_capability_question`）：
   最高频的「上手第一问」完全不需要查实时数据，本地生成零延迟、零模型依赖。
   实测 0.001s。
2. **能映射到只读命令的疑问句**：本地即时查（确定性路径）。
3. **真需要 LLM 的问题 → 先回执 + 后台推送**（`allow_defer`）：
   企微回调传 `allow_defer=True`，LLM 转后台线程，算完由后台主动推送。
   全局单工（`_DEFER_BUSY`）防并发打爆模型、防连发刷屏。

本测试锁定上述契约。
"""

import ast
import os
import sys
import tempfile
import time
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


print("[1] 能力问句即时化（notify.py）")

check("_looks_like_capability_question 存在",
      "def _looks_like_capability_question(" in SRC_NT)
check("_CAPABILITY_MARKERS 词表存在", "_CAPABILITY_MARKERS = (" in SRC_NT)
check("词表覆盖「你可以做什么/你能做什么/有什么功能/怎么用/帮助/菜单/help」",
      all(m in SRC_NT for m in ("你可以做什么", "你能做什么", "有什么功能",
                                "怎么用", "帮助", "菜单", "help")))
check("能力问句在疑问句分支内**最先**判定（1a）",
      SRC_NT.index("if _looks_like_capability_question(text, low):")
      < SRC_NT.index("if allow_defer and _assistant_available():"),
      "能力类必须比 LLM 延后更早，否则仍会走 LLM 踩 5 秒红线")
check("能力问句返回助手说明（本地生成）",
      "return _cmd_assistant_help()" in SRC_NT)
check("不再有错位的 1.6 块（已并入 1a）",
      "---- 1.6 能力/菜单类问句" not in SRC_NT)

print("\n[2] LLM 延后推送（notify.py）")

check("wecom_command_reply 接受 allow_defer",
      "allow_defer: bool = False" in SRC_NT)
check("allow_defer 且助手可用才延后",
      "if allow_defer and _assistant_available():" in SRC_NT)
check("_defer_assistant_reply 存在", "def _defer_assistant_reply(" in SRC_NT)
check("_assistant_available 存在", "def _assistant_available(" in SRC_NT)
check("_push_to_channel 存在", "def _push_to_channel(" in SRC_NT)
check("defer_stats 存在（可观测）", "def defer_stats(" in SRC_NT)
check("延后用守护线程", "threading.Thread(" in SRC_NT and "daemon=True" in SRC_NT)
check("全局单工防并发（_DEFER_BUSY）", "_DEFER_BUSY" in SRC_NT)
check("单工锁（_DEFER_LOCK）", "_DEFER_LOCK" in SRC_NT)
check("回执文案含「正在查询」", "正在查询" in SRC_NT)
check("延后入口有异常兜底（绝不拖累回调）",
      "try:" in SRC_NT[SRC_NT.index("def _defer_assistant_reply"):]
      and "except Exception:" in SRC_NT[SRC_NT.index("def _defer_assistant_reply"):])

print("\n[3] 企微回调接线（web_proxy.py）")

check("回调改 MP 模式（立即返回 + 后台处理，不再传 allow_defer）",
      "_wecom_handle_async" in SRC_WP
      and "threading.Thread(" in SRC_WP
      and "wecom_command_reply(cmd, session_id, False)" in SRC_WP)
check("后台处理明确传 allow_defer=False（已是后台，无需再回执）",
      "wecom_command_reply(cmd, session_id, False)" in SRC_WP)
check("回调注释说明 MP 模式根因",
      "MP 模式" in SRC_WP or "submit_message_to_host" in SRC_WP)

print("\n[4] 行为验证（真实函数，不联网）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
os.environ["AB_ASSISTANT_WEBUI"] = "http://127.0.0.1:1"   # 不联网
os.environ["AB_ASSISTANT_GATEWAY"] = "http://127.0.0.1:1"
sys.path.insert(0, str(ROOT))
from gateway import notify as N  # noqa: E402

# 4a. 能力问句判定
cap_cases = [
    "你可以做什么", "你能做什么", "你能干什么", "你会什么", "你能帮我",
    "有什么功能", "有哪些功能", "功能列表", "怎么用", "如何使用",
    "使用说明", "指令", "命令列表", "帮助", "菜单", "help",
    "what can you do",
]
for q in cap_cases:
    got = N._looks_like_capability_question(q)
    check(f"能力问句 {q!r} -> True", got is True, f"got {got}")

non_cap = [
    "现在积分多少", "今天执行过任务吗", "哪个账号余额最高",
    "模型有哪些", "你好", "网关状态怎么样",
]
for q in non_cap:
    got = N._looks_like_capability_question(q)
    check(f"非能力问句 {q!r} -> False", got is False, f"got {got}")

# 4b. 能力问句**即时**回复且不走 LLM（关键：修的就是 9 秒超时）
t0 = time.time()
r = N.wecom_command_reply("你可以做什么", session_id="speed")
dt = time.time() - t0
check("「你可以做什么」< 1 秒（原 9 秒被企微丢弃）", dt < 1.0, f"got {dt:.2f}s")
check("「你可以做什么」返回助手说明", bool(r) and "智能助手" in r, f"got {(r or '')[:60]}")

for q in ("你能做什么", "有什么功能", "怎么用", "帮助", "help"):
    t1 = time.time()
    rq = N.wecom_command_reply(q, session_id="speed")
    dtq = time.time() - t1
    check(f"{q!r} 即时回复（{dtq:.3f}s）", dtq < 1.0 and "智能助手" in (rq or ""))

# 4c. 延后推送：单工 + 回执（用假 worker 验证框架，不打真 LLM）
N._DEFER_BUSY = False
t2 = time.time()
ok1 = N._defer_assistant_reply("测试问题", "defer-sess", channel="__nonexistent__")
t_defer = time.time() - t2
check("延后入口立即返回（不阻塞）", ok1 is True and t_defer < 0.5, f"got {ok1} {t_defer:.3f}s")
check("延后期间 _DEFER_BUSY=True", N.defer_stats().get("busy") is True)
ok2 = N._defer_assistant_reply("第二个问题", "defer-sess2", channel="__nonexistent__")
check("单工：第二次被拒（防并发/刷屏）", ok2 is False)
# 等 worker 结束，把状态复位
for _ in range(50):
    if not N.defer_stats().get("busy"):
        break
    time.sleep(0.2)
check("worker 结束后 busy 复位", N.defer_stats().get("busy") is False)
ok3 = N._defer_assistant_reply("第三个问题", "defer-sess3", channel="__nonexistent__")
check("复位后可再次延后", ok3 is True)
for _ in range(50):
    if not N.defer_stats().get("busy"):
        break
    time.sleep(0.2)

# 4d. 延后推送的渠道兜底：不存在的渠道不能炸
check("推送未知渠道返回 False 而非抛异常",
      N._push_to_channel("__nonexistent__", "hi") is False)

# 4e. allow_defer=False 时行为不变（向后兼容）
r5 = N.wecom_command_reply("现在积分多少", session_id="nodefer")
check("allow_defer=False 不返回回执文案",
      not (r5 and "正在查询" in r5))

# 4f. 写操作确认不受影响（回归）
N.clear_session("wf") if hasattr(N, "clear_session") else None
from gateway import assistant as _A5  # noqa: E402
_A5.clear_session("wf")
r6 = N.wecom_command_reply("执行任务", session_id="wf")
check("写操作仍先要确认（未被延后逻辑破坏）",
      bool(r6) and "待确认" in r6, f"got {(r6 or '')[:40]}")

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
