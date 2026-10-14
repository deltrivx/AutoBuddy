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
# 写端点必须不在工具集。注意 `/api/wb-daily/runs`（读历史）含子串
# `wb-daily/run`，旧断言把它误判成写端点 —— 用带引号的精确串判定。
# 阶段 5 后，写端点（wb-daily/run、checkin/all）从「完全不存在」变为
# 「只存在于 _WRITE_OPS 执行器、且必须经两段式确认」。因此判据要从
# 「文件里没有」改为「工具区（_tool_* 函数）里没有」—— 工具是只读的，
# 写操作走独立的确认流程（_test_write_confirm.py 锁定）。
_tools_start = SRC_AS.index("def _tool_credits")
_tools_end = SRC_AS.index("def _tool_registry")
_TOOLS_SRC = SRC_AS[_tools_start:_tools_end]
check("工具区无写端点（启动任务）", '"/api/wb-daily/run"' not in _TOOLS_SRC)
check("工具区无写端点（签到）", "checkin/all" not in _TOOLS_SRC)
check("只读历史端点 runs 在工具集（回答「今天跑过吗」）",
      "/api/wb-daily/runs" in _TOOLS_SRC)
check("写端点只出现在写操作执行器里（经确认流程）",
      '"/api/wb-daily/run"' in SRC_AS and "def _op_run_daily" in SRC_AS)

print("\n[3] 集成契约（notify.py）")

check("_assistant_fallback 存在", "def _assistant_fallback(" in SRC_NT)
check("命令路由尾部调用助手", "reply = _assistant_fallback(text, session_id)" in SRC_NT)
check("wecom_command_reply 接受 session_id 与 allow_defer",
      "def wecom_command_reply(cmd: str, session_id: Optional[str] = None,\n                        allow_defer: bool = False)" in SRC_NT)
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
      "回复「帮助」查看可用指令" in SRC_NT)

print("\n[4] web_proxy 不阻塞事件循环")

check("run_in_threadpool 跑命令路由", "run_in_threadpool(" in SRC_WP)
check("传入 from_user 作为会话 id", "from_user or None" in SRC_WP)
check("web_proxy 无第二份命令语义（历史教训）",
      'elif cmd == "accounts"' not in SRC_WP and 'elif cmd == "models"' not in SRC_WP)
# 「无第二份助手实现」的正确判据：web_proxy 不得内含助手核心逻辑
# （系统提示词 / 工具注册表 / 工具分发 / 自己的命令分支）。
# 允许且应当**委托** —— 连通性测试要调 _assistant.assistant_reply()。
check("web_proxy 无第二份助手实现（无 SYSTEM_PROMPT/工具表/分发）",
      "SYSTEM_PROMPT" not in SRC_WP
      and "_tool_registry" not in SRC_WP
      and "_dispatch_tool" not in SRC_WP)
check("web_proxy 只委托 assistant（import + 调用 probe）",
      "import assistant as _assistant" in SRC_WP
      and "_assistant.assistant_reply(" in SRC_WP)

print("\n[5] 真实模块行为（不依赖网络）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
sys.path.insert(0, str(ROOT))
from gateway import assistant as A  # noqa: E402

check("模块可导入", A is not None)
check("17 个工具已注册（阶段4 全覆盖 + 阶段6 主动智能）",
      len(A._TOOLS) == 17, f"got {len(A._TOOLS)}")

# 阶段 4 新增的 9 个只读工具
STAGE4_TOOLS = (
    "get_account_pool", "get_selection_stats", "get_disabled_models",
    "get_health_check", "get_account_credentials", "get_task_ledger",
    "get_credit_breakdown", "get_api_keys", "get_notify_status",
)
for _t in STAGE4_TOOLS:
    check(f"阶段4 工具 {_t} 已注册", _t in A._TOOLS)

# 阶段 6 新增的 2 个主动智能工具
STAGE6_TOOLS = ("get_credit_trend", "diagnose_task_failure")
for _t in STAGE6_TOOLS:
    check(f"阶段6 工具 {_t} 已注册", _t in A._TOOLS)
check("阶段6 归因工具不把 running 误判为失败（线上实测误报已修）",
      '_OK_OR_RUNNING = ("done", "running", "pending")' in SRC_AS)
check("阶段4 工具全部只读（工具区无写端点）",
      '"/api/wb-daily/run"' not in _TOOLS_SRC
      and "checkin/all" not in _TOOLS_SRC
      and '"/api/wb-daily/abort/' not in _TOOLS_SRC,
      "写端点只能存在于 _WRITE_OPS 执行器（经两段式确认），不得出现在工具区")
check("禁用明细读权威来源 modelPolicy（非可用全集）",
      'd.get("modelPolicy")' in SRC_AS)
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

# ---------------------------------------------------------------------------
# [6] Provider 注册表与配置层（第三阶段：设置页可视化配置）
# 参考 MoviePilot 的 LLMProviderInfo / ProviderSpec。
# ---------------------------------------------------------------------------
print("\n[6] Provider 注册表（对齐 MoviePilot LLMProviderInfo）")

reg = A.PROVIDERS
keys = [p["key"] for p in reg]
check("注册表非空且 key 唯一", len(keys) == len(set(keys)) and keys, f"got {keys}")
check("默认自带 provider（gateway）", "gateway" in keys)
check("支持自定义 provider（openai 兼容）", "openai" in keys)

for p in reg:
    for field in ("key", "name", "desc", "default_base_url",
                  "base_url_editable", "requires_base_url",
                  "supports_api_key", "supports_model_refresh"):
        check(f"provider {p['key']} 有字段 {field}", field in p,
              f"missing {field}")
    check(f"provider {p['key']} base_url_presets 是列表",
          isinstance(p.get("base_url_presets"), list))
    if p.get("base_url_presets"):
        check(f"provider {p['key']} 每个 preset 有 id/label/value",
              all(set(["id", "label", "value"]) <= set(pre.keys())
                  for pre in p["base_url_presets"]))
    # secret 语义要求：需要密钥的 provider 必须带标签与提示
    if p.get("supports_api_key"):
        check(f"provider {p['key']} 有 api_key_label", bool(p.get("api_key_label")))
        check(f"provider {p['key']} 有 api_key_hint", bool(p.get("api_key_hint")))

check("默认 provider 取 gateway", A._default_provider() == "gateway")
check("_PROVIDER_BY_KEY 索引与注册表一致",
      set(A._PROVIDER_BY_KEY.keys()) == set(keys))

print("\n[7] 配置读写与 secret 语义（真实文件）")

# 全新配置：默认值
cfg0 = A._load_config()
check("默认 enabled=True", cfg0.get("enabled") is True)
check("默认 provider=gateway", cfg0.get("provider") == "gateway")
check("默认 model 为空（自动挑选）", cfg0.get("model") == "")
check("默认 providers 覆盖注册表全部 provider",
      set(cfg0.get("providers", {}).keys()) >= set(keys))

# 保存 provider + 地址 + 密钥
A._save_config({"provider": "openai",
                "providers": {"openai": {"base_url": "https://api.deepseek.com/v1",
                                          "api_key": "sk-test-123"}}})
rt = A._runtime()
check("runtime 反映新 provider", rt.get("provider") == "openai")
check("runtime 反映新 base_url", rt.get("base_url") == "https://api.deepseek.com/v1")
check("runtime 读到 api_key", rt.get("api_key") == "sk-test-123")

# secret 语义 1：不提交 = 保留
A._save_config({"model": "deepseek-chat"})
rt2 = A._runtime()
check("不提交 api_key 时保留原值", rt2.get("api_key") == "sk-test-123")
check("model 已更新", rt2.get("model") == "deepseek-chat")

# secret 语义 2：空串 = 清除
A._save_config({"providers": {"openai": {"api_key": ""}}})
check("提交空串 = 清除密钥", A._runtime().get("api_key") == "")

# secret 语义 3：GET 视图永不回明文
v = A.public_view()
ok_secret = True
for pk, slot in v["config"]["providers"].items():
    ak = slot.get("api_key")
    if not (isinstance(ak, dict) and set(ak.keys()) == {"has_value"}):
        ok_secret = False
check("public_view 的 api_key 只回 {has_value}", ok_secret,
      f"got {v['config']['providers']}")

# 未配置时 has_value=False
A._save_config({"provider": "gateway"})
v2 = A.public_view()
check("未配置密钥时 has_value=False",
      v2["config"]["providers"]["gateway"]["api_key"] == {"has_value": False})

# enabled 开关贯通到 runtime
A._save_config({"enabled": False})
check("enabled=False 时 runtime 不可用", A._runtime().get("enabled") is False)
check("enabled=False 时 session_stats.enabled=False",
      A.session_stats().get("enabled") is False)
check("enabled=False 时 assistant_reply 返回空串",
      A.assistant_reply("s", "你好") == "")
A._save_config({"enabled": True})
check("恢复 enabled=True", A._runtime().get("enabled") is True)

# systemPrompt 可覆盖
A._save_config({"systemPrompt": "你是测试助手"})
check("systemPrompt 可保存并贯通 runtime",
      A._runtime().get("systemPrompt") == "你是测试助手")

# 配置持久化（重新加载仍在）
A._save_config({"model": "persist-check"})
reloaded = A._load_config()
check("配置落盘后可重新读取", reloaded.get("model") == "persist-check")

# base_url 反斜杠归一化（尾斜杠去掉，避免 //v1/...）
A._save_config({"providers": {"gateway": {"base_url": "http://127.0.0.1:18091/"}}})
check("base_url 去掉尾斜杠",
      A._runtime().get("base_url") == "http://127.0.0.1:18091")

# _pick_model：显式指定优先
A._save_config({"provider": "gateway", "model": "hy3"})
check("_pick_model 用设置页显式指定的模型", A._pick_model() == "hy3")

print("\n[8] 设置页接口契约（web_proxy）")
check("GET /api/assistant 存在", '@app.get("/api/assistant")' in SRC_WP)
check("PUT /api/assistant 存在", '@app.put("/api/assistant")' in SRC_WP)
check("POST /api/assistant/test 存在", '@app.post("/api/assistant/test")' in SRC_WP)
check("POST /api/assistant/models 存在", '@app.post("/api/assistant/models")' in SRC_WP)
check("GET 返回 public_view（脱敏）", "_assistant.public_view()" in SRC_WP)
check("保存走 _save_config（合并式）", "_assistant._save_config(" in SRC_WP)
check("测试返回 ok/durationMs/replyPreview（MP 的 LLMTestResult）",
      "durationMs" in SRC_WP and "replyPreview" in SRC_WP)
check("模型清单拉 /v1/models 并带 key",
      'base + "/v1/models"' in SRC_WP and "api_key=key" in SRC_WP)
check("助手 section 已注入设置页", "settings-assistant" in SRC_WP)
check("设置页刷新会重取助手配置", "injectAssistant()" in SRC_WP)
check("通知区在助手区之后注入（DOM 顺序：API→巡检→助手→通知）",
      SRC_WP.index("injectAssistant();") < SRC_WP.index("injectNotify();"))

# ⚠️ 2026-10-14 线上修复：助手区块首次加载不显示。
# 根因：`injectAssistant()` 只加进了 `wbRefreshApi()`（刷新路径），
# 漏了 `run()`（DOMContentLoaded 首次加载路径）—— 用户实测
# 「设置页没有智能助手区块」，只有手动触发刷新后才出现。
# 因此必须校验**两条入口**都注入了助手，且四个 section 顺序一致。
_js = None
try:
    import re as _re0
    _blocks = _re0.findall(r"<script[^>]*>(.*?)</script>", SRC_WP, _re0.S)
    _js = "\n".join(_blocks)
except Exception:
    _js = SRC_WP


def _entry_has_all_injects(body: str) -> bool:
    return all(("inject%s();" % n) in body
               for n in ("ApiAccess", "ModelHealth", "Assistant", "Notify"))


# 入口 1：run() —— 定位 DOMContentLoaded 之前的那个函数体
_run_idx = _js.index("window.addEventListener(\"DOMContentLoaded\"")
_run_body = _js[:_run_idx]
_run_tail = _run_body[_run_body.rindex("function run()"):]
check("首次加载入口 run() 注入了助手（线上漏点）",
      "injectAssistant();" in _run_tail,
      "run() 内未见 injectAssistant()")
check("首次加载入口 run() 四个 section 齐全", _entry_has_all_injects(_run_tail))

# 入口 2：wbRefreshApi()
_ref_idx = _js.index("function wbRefreshApi(")
_ref_tail = _js[_ref_idx:]
check("刷新入口 wbRefreshApi() 注入了助手",
      "injectAssistant();" in _ref_tail)
check("刷新入口 wbRefreshApi() 四个 section 齐全", _entry_has_all_injects(_ref_tail))

# 两条入口的 section 顺序必须一致（否则刷新前后区块位置会跳）
_order = ["injectApiAccess();", "injectModelHealth();",
          "injectAssistant();", "injectNotify();"]


def _order_ok(body: str) -> bool:
    pos = [body.index(x) for x in _order if x in body]
    return len(pos) == 4 and pos == sorted(pos)


check("两条入口的 section 顺序一致", _order_ok(_run_tail) and _order_ok(_ref_tail))

import shutil
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
