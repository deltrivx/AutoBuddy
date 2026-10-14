#!/usr/bin/env python3
"""交互智能助手回归（2026-10-14 第二阶段）。

### 需求

用户要求「给交互加入智能助手，以实现更多功能，也可以参考 MoviePilot 实现」。

### 参考 MoviePilot 的 app/agent/llm/

| MoviePilot | 本实现对应 |
|---|---|
| `tools.py` 工具注册表（能力声明 + 解析） | `assistant._TOOLS` 注册表 + `_tool_registry()` |
| 明确的 tool 边界 | 每 tool 独立 try/except，异常转可读文本 |
| 会话隔离 | 按 `FromUserName`/`chat_id` 分会话，TTL 过期 |
| 命令与自然语言分层 | **命令优先，LLM 兜底** |

### 设计取舍

1. **命令优先，LLM 兜底**：9 个既有命令是确定性路径（零延迟、不依赖模型），
   永不被 LLM 故障拖累；助手只接管未命中命令的自然语言。
2. **复用自家网关供电**：经 loopback（127.0.0.1:18091）调自家网关，
   `_gateway_auth` 对 loopback 免鉴权；天然继承账号池调度/冷却/配额。
3. **只读工具**：助手不执行写操作（启动任务/签到/开关通知），
   避免自然语言歧义导致误操作；写操作仍走显式命令。
4. **可一键关闭**：`AB_ASSISTANT_ENABLED=0` 立即退回纯命令模式。
5. **绝不阻塞事件循环**：web_proxy 回调用 `run_in_threadpool` 跑
   （LLM 最长 45s）。

### 历史教训的规避

v0.9.63 曾因「web_proxy 内联一份命令链 + notify 另一份」导致改了不生效。
本实现把命令语义**全部**留在 `notify.wecom_command_reply`，
`assistant.py` 只提供 `assistant_reply()` 一个入口，无第二份路由。

本测试锁定上述契约。
"""

import ast
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
SRC_AS = (ROOT / "gateway" / "assistant.py").read_text(encoding="utf-8")
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


print("[1] 模块契约（assistant.py）")

check("ASSISTANT_ENABLED 常量", "ASSISTANT_ENABLED = " in SRC_AS)
check("默认开启", 'AB_ASSISTANT_ENABLED", "1"' in SRC_AS)
check("ASSISTANT_TIMEOUT 存在", "ASSISTANT_TIMEOUT = " in SRC_AS)
check("SESSION_TTL 存在", "SESSION_TTL = " in SRC_AS)
check("MAX_SESSIONS 存在", "MAX_SESSIONS = " in SRC_AS)
check("assistant_reply 入口存在", "def assistant_reply(" in SRC_AS)
check("is_command 存在（命令优先的判据）", "def is_command(" in SRC_AS)
check("session_stats 存在（可观测）", "def session_stats(" in SRC_AS)
check("clear_session 存在（可重置）", "def clear_session(" in SRC_AS)
check("会话 GC（TTL 清理）", "def _gc_sessions_locked(" in SRC_AS)
check("历史轮数限制", "HISTORY_TURNS = " in SRC_AS)
check("输出长度封顶", "MAX_OUTPUT_CHARS = " in SRC_AS)
check("loopback 供电（不经公网）", 'http://127.0.0.1:18091' in SRC_AS)
check("本地工具端点（loopback 18090）", 'http://127.0.0.1:18090' in SRC_AS)

print("\n[2] 工具集（只读）")

for tool in ("get_credits", "get_accounts", "get_models", "get_status",
             "get_task_progress", "get_request_stats"):
    check(f"工具 {tool} 已注册", f'"{tool}":' in SRC_AS)
check("_dispatch_tool 异常隔离", "def _dispatch_tool(" in SRC_AS)
check("工具调用上限 3 个（防刷）", "tool_calls[:3]" in SRC_AS)
check("工具结果长度封顶", "[:MAX_OUTPUT_CHARS]" in SRC_AS)
check("tool_echo 识别（hy3 实测回吐 tool_calls 原文）", "_is_tool_echo" in SRC_AS)
check("tool_echo 正则匹配 <tool_calls / <tool_call",
      'r"^\\s*<\\s*tool_call[s]?\\b"' in SRC_AS)
check("tool_echo 判为无效输出（回落到工具结果）",
      "if _is_tool_echo(final):" in SRC_AS and "final = \"\"" in SRC_AS)
check("第二轮用 tool_choice=none 强制出正文", 'payload2["tool_choice"] = "none"' in SRC_AS)
check("tool_choice=none 不被支持时退回无 tools 重试",
      'payload2.pop("tools", None)' in SRC_AS)
check("第二轮模型又发起工具调用时不采纳正文",
      "if not (msg2.get(\"tool_calls\") or []):" in SRC_AS)
# 只读保证：工具函数里不得出现 POST/PUT/DELETE 语义的调用
check("工具全部只读（无 POST/PUT 调用）",
      "_tool_credits" in SRC_AS
      and 'method="POST"' not in SRC_AS[SRC_AS.index("def _tool_credits"):
                                          SRC_AS.index("def _tool_registry")])
check("启动任务/签到不在工具集（写操作须走命令）",
      "wb-daily/run" not in SRC_AS and "checkin/all" not in SRC_AS)

print("\n[3] 集成契约（notify.py）")

check("_assistant_fallback 存在", "def _assistant_fallback(" in SRC_NT)
check("命令路由尾部调用助手", "reply = _assistant_fallback(text, session_id)" in SRC_NT)
check("wecom_command_reply 接受 session_id",
      "def wecom_command_reply(cmd: str, session_id: Optional[str] = None)" in SRC_NT)
check("_wecom_interpret 透传 session_id",
      "session_id: Optional[str] = None" in SRC_NT[SRC_NT.index("def _wecom_interpret"):]
      and "wecom_command_reply(content or \"\", session_id=session_id)" in SRC_NT)
check("wecom_callback_message 传 from_user",
      "_wecom_interpret(msg_type, content, event_key,\n                                      session_id=from_user)" in SRC_NT)
check("Telegram callback 传 chat_id", "session_id=chat_id) or \"未知操作\"" in SRC_NT)
check("Telegram message 传 chat_id",
      '_wecom_interpret("text", cmd_text, session_id=chat_id)' in SRC_NT)
check("「助手」命令分支存在", '"助手" in text or low in ("assistant", "ai")' in SRC_NT)
check("_cmd_assistant_help 存在", "def _cmd_assistant_help()" in SRC_NT)
check("菜单新增智能助手按钮", '("AB_ASSISTANT", "智能助手",   "常用")' in SRC_NT)
check("菜单 key 映射到「助手」", '"AB_ASSISTANT": "助手",' in SRC_NT)
check("help 文本说明助手", "智能助手查实时数据回答" in SRC_NT)
check("助手不可用时回落原兜底文案",
      "回复「帮助」查看可用指令，或直接提问" in SRC_NT)

print("\n[4] web_proxy 不阻塞事件循环")

check("run_in_threadpool 跑命令路由", "run_in_threadpool(" in SRC_WP)
check("传入 from_user 作为会话 id", "from_user or None" in SRC_WP)
check("web_proxy 无第二份命令语义（历史教训）",
      'elif cmd == "accounts"' not in SRC_WP and 'elif cmd == "models"' not in SRC_WP)
check("web_proxy 无第二份助手实现", "assistant_reply" not in SRC_WP)

print("\n[5] 真实模块行为（不依赖网络）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
sys.path.insert(0, str(ROOT))
from gateway import assistant as A  # noqa: E402

check("模块可导入", A is not None)
check("6 个工具已注册", len(A._TOOLS) == 6, f"got {len(A._TOOLS)}")
check("_tool_registry 返回 OpenAI function 格式",
      all(t.get("type") == "function" and "function" in t for t in A._tool_registry()))
check("每个工具都有 description",
      all(t["function"].get("description") for t in A._tool_registry()))

# is_command：命令优先的判据必须与 wecom_command_reply 的命令分支一致
cases = [("账号余额", True), ("models", True), ("status", True), ("credits", True),
         ("现在积分多少", True), ("帮助", True), ("通知 on", True),
         ("今天天气怎么样", False), ("你会什么", False), ("你好", False)]
for text, want in cases:
    got = A.is_command(text)
    check(f"is_command({text!r}) == {want}", got == want, f"got {got}")

# 会话隔离
A.clear_session("u1")
A._remember("u1", "user", "第一轮问题")
A._remember("u1", "assistant", "第一轮回答")
A._remember("u2", "user", "别人的问题")
h1 = A._history("u1")
h2 = A._history("u2")
check("同会话历史可读", len(h1) == 2, f"got {len(h1)}")
check("不同会话互相隔离", len(h2) == 1 and h2[0]["content"] == "别人的问题")
check("会话统计可观测", A.session_stats().get("sessions", 0) >= 2)
A.clear_session("u1")
check("clear_session 生效", len(A._history("u1")) == 0)

# TTL 过期
A._SESSIONS["old"] = {"history": [{"role": "user", "content": "x"}], "updated": time.time() - (A.SESSION_TTL + 100)}
A._gc_sessions_locked(time.time())
check("过期会话被清理", "old" not in A._SESSIONS)

# 工具异常隔离：传错误参数不应抛
out = A._dispatch_tool("get_accounts", {"limit": "not-a-number"})
check("工具异常被转成可读文本", isinstance(out, str) and "失败" in out or "账号" in out,
      f"got {out[:80]}")
out2 = A._dispatch_tool("nonexistent_tool", {})
check("未知工具返回提示而非抛异常", "工具不存在" in out2)

# 关闭开关时直接返回空
_old = A.ASSISTANT_ENABLED
A.ASSISTANT_ENABLED = False
check("关闭开关后 assistant_reply 返回空串", A.assistant_reply("s", "你好") == "")
A.ASSISTANT_ENABLED = _old

# tool_echo 识别（v0.9.67 线上实测：hy3 第二轮把工具调用原文当正文回吐）
echo_cases = [
    ("<tool_calls:6124c78e>", True),
    ("<tool_call:6124c78e>get_status", True),
    ("  <tool_calls:abc>  多余空白", True),
    ("剩余积分：41,023.50", False),
    ("网关状态：healthy", False),
    ("<b>这不是工具调用</b>", False),
    ("", False),
]
for text, want in echo_cases:
    got = A._is_tool_echo(text)
    check(f"_is_tool_echo({text[:24]!r}) == {want}", got == want, f"got {got}")

import shutil
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
