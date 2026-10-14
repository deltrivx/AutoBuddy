#!/usr/bin/env python3
"""交互智能助手（2026-10-14 第二阶段）。

### 需求

用户要求「给交互加入智能助手，以实现更多功能，也可以参考 MoviePilot 实现」。

### 参考 MoviePilot 的设计（app/agent/llm/）

| MoviePilot | 本模块对应实现 |
|---|---|
| `tools.py` 工具注册表（能力声明 + 解析） | `_TOOLS` 注册表 + `_tool_registry()` |
| 明确的 tool 边界（哪些模型支持哪些工具） | 每个 tool 带 `enabled` 开关与独立异常处理 |
| 会话隔离 | 按 `chat_id`/`FromUserName` 分会话，TTL 过期 |
| 命令与自然语言分层 | **命令优先**，未命中命令才进 LLM |

### 设计取舍（为什么这样做）

1. **命令优先，LLM 兜底**：原有 9 个命令是确定性路径，零成本、零延迟、
   不会因模型限流而失效。智能助手只接管「未命中命令」的自然语言，
   保证既有功能永不被 LLM 故障拖累。
2. **复用自家网关供电**：助手通过 loopback（127.0.0.1:18091）调自家网关，
   `_gateway_auth` 对 loopback 免鉴权，无需额外密钥；
   且天然继承账号池调度、冷却、配额 —— 助手不占额外资源，
   也不受单账号限流影响（换号重试已由网关处理）。
3. **工具调用走本地 HTTP**：助手的工具只读**本容器内**端点
   （127.0.0.1:18090），不经公网，无凭证外泄风险。
4. **可一键关闭**：`AB_ASSISTANT_ENABLED=0` 立即退回纯命令模式，
   不依赖模型可用性。

### 工具集（首版）

- `get_credits` —— 积分统计（剩余/今日/近7天/本月）
- `get_accounts` —— 账号余额明细
- `get_models` —— 当前模型与禁用分类
- `get_status` —— 网关与账号池状态
- `get_task_progress` —— 每日任务进度
- `get_request_stats` —— 请求指标（成功率/耗时）

每个工具都是**只读**的：助手不执行任何写操作（启动任务/签到/开关通知），
避免自然语言歧义导致误操作。写操作仍走显式命令。
"""

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

ASSISTANT_ENABLED = (os.getenv("AB_ASSISTANT_ENABLED", "1") or "1").strip() not in ("0", "false", "no")
ASSISTANT_MODEL = (os.getenv("AB_ASSISTANT_MODEL", "") or "").strip()
ASSISTANT_TIMEOUT = float(os.getenv("AB_ASSISTANT_TIMEOUT", "45") or 45)
SESSION_TTL = float(os.getenv("AB_ASSISTANT_SESSION_TTL", "1800") or 1800)
MAX_SESSIONS = int(os.getenv("AB_ASSISTANT_MAX_SESSIONS", "200") or 200)
HISTORY_TURNS = int(os.getenv("AB_ASSISTANT_HISTORY", "6") or 6)
MAX_OUTPUT_CHARS = 1200

# 本地端点（同容器 loopback，不经公网）
WEBUI_BASE = os.getenv("AB_ASSISTANT_WEBUI", "http://127.0.0.1:18090")
GATEWAY_BASE = os.getenv("AB_ASSISTANT_GATEWAY", "http://127.0.0.1:18091")

# 兜底模型（按优先级尝试）。留空则由运行时从 /v1/models 动态挑选。
_FALLBACK_MODELS = ("hy3", "deepseek-v3", "kimi-k3", "deepseek-chat")

SYSTEM_PROMPT = """你是 AutoBuddy 网关的智能助手「小助手」，运行在企微/Telegram 交互里。

职责：用**中文**回答用户关于网关、账号积分、模型、任务、用量的问题。

你能看到的数据通过工具获取，工具返回的是真实实时数据 —— **严格依据工具结果回答，
不要编造数字**。如果工具返回错误或空数据，如实说明「暂时查不到」，不要猜测。

工具返回的文本本身就是可读格式，你可以直接摘录关键数字组织回答，
也可以补充一句简短解读（如「余额偏低，建议关注」）。

约束：
- 回答**简洁**，一般不超过 6 行；用户要明细时再展开。
- 不执行写操作（启动任务/签到/开关通知）—— 那些请引导用户用命令。
- 不要输出 Markdown 标题、代码块；用 plain text + 短横线列表。
- 不知道就说不知道，不要装懂。"""


# ---------------------------------------------------------------------------
# 会话管理（按会话维度隔离历史，TTL 过期）
# ---------------------------------------------------------------------------

_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SESSIONS_LOCK = threading.Lock()


def _session_key(session_id: str) -> str:
    return str(session_id or "default")[:80]


def _gc_sessions_locked(now: Optional[float] = None) -> None:
    now = time.time() if now is None else float(now)
    for k in [k for k, v in _SESSIONS.items() if (v.get("updated") or 0) + SESSION_TTL < now]:
        _SESSIONS.pop(k, None)
    # 超出上限时淘汰最旧的
    if len(_SESSIONS) > MAX_SESSIONS:
        for k in sorted(_SESSIONS, key=lambda x: _SESSIONS[x].get("updated") or 0)[:-MAX_SESSIONS]:
            _SESSIONS.pop(k, None)


def _history(session_id: str) -> List[Dict[str, str]]:
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(_session_key(session_id)) or {}
        return list(s.get("history") or [])


def _remember(session_id: str, role: str, content: str) -> None:
    key = _session_key(session_id)
    now = time.time()
    with _SESSIONS_LOCK:
        _gc_sessions_locked(now)
        s = _SESSIONS.setdefault(key, {"history": [], "updated": now})
        s["history"] = (s.get("history") or []) + [{"role": role, "content": str(content)[:2000]}]
        # 只保留最近 N 轮（一问一答算 2 条）
        keep = max(HISTORY_TURNS * 2, 2)
        if len(s["history"]) > keep:
            del s["history"][:-keep]
        s["updated"] = now


def clear_session(session_id: str) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.pop(_session_key(session_id), None)


def session_stats() -> Dict[str, Any]:
    with _SESSIONS_LOCK:
        return {"enabled": ASSISTANT_ENABLED, "sessions": len(_SESSIONS),
                "model": ASSISTANT_MODEL or "(auto)", "ttlSec": SESSION_TTL}


# ---------------------------------------------------------------------------
# 工具集（只读）
# ---------------------------------------------------------------------------

def _http_json(url: str, timeout: float = 8.0, method: str = "GET",
               body: Optional[bytes] = None) -> Any:
    import urllib.request
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _fmt_num(v: Any) -> str:
    try:
        f = float(v)
        return "{:,.2f}".format(f) if f % 1 else "{:,.0f}".format(f)
    except (TypeError, ValueError):
        return "?"


def _tool_credits(_args: Dict[str, Any]) -> str:
    s = (_http_json(WEBUI_BASE + "/api/credits/stats") or {}).get("summary") or {}
    return ("积分统计：\n"
            "  · 剩余积分：%s\n"
            "  · 今日消耗：%s\n"
            "  · 近 7 天消耗：%s\n"
            "  · 本月消耗：%s" % (
                _fmt_num(s.get("currentRemaining")), _fmt_num(s.get("usageToday")),
                _fmt_num(s.get("usage7Days")), _fmt_num(s.get("usageThisMonth"))))


def _tool_accounts(args: Dict[str, Any]) -> str:
    limit = int(args.get("limit") or 23)
    accs = (_http_json(WEBUI_BASE + "/api/credits/stats") or {}).get("accounts") or []
    if not accs:
        return "暂无账号数据"
    lines = ["账号余额（%d 个）：" % len(accs)]
    total = 0.0
    for a in accs[:max(1, min(limit, 50))]:
        name = str(a.get("accountName") or a.get("accountId") or "?")
        rem = a.get("currentRemaining")
        try:
            total += float(rem)
        except (TypeError, ValueError):
            pass
        lines.append("  %s: %s" % (name[:16], _fmt_num(rem)))
    lines.append("  合计：%s" % _fmt_num(total))
    return "\n".join(lines)


def _tool_models(_args: Dict[str, Any]) -> str:
    g = _http_json(WEBUI_BASE + "/api/gateway-info") or {}
    m = g.get("models") or {}
    src = g.get("disabledBySource") or {}
    ids: List[str] = []
    try:
        am = _http_json(WEBUI_BASE + "/api/account-models") or {}
        ids = [str(x) for x in (am.get("catalog") or []) if x]
    except Exception:
        pass
    if not ids:
        ids = [str(x) for x in (m.get("sample") or []) if x]
    out = ("当前模型：\n"
           "  · 正常调用：%s 个（自动发现 %s 个）\n"
           "  · 轮检禁用：%s 个组合\n"
           "  · 手动禁用：%s 个组合" % (
               m.get("count", "?"), m.get("autoDiscovered", "?"),
               src.get("auto") or 0, src.get("manual") or 0))
    if ids:
        out += "\n  · 清单：%s" % ", ".join(ids[:30])
        if len(ids) > 30:
            out += " …等 %d 个" % len(ids)
    return out


def _tool_status(_args: Dict[str, Any]) -> str:
    h = _http_json(GATEWAY_BASE + "/health") or {}
    info = _http_json(WEBUI_BASE + "/api/gateway-info") or {}
    acc = info.get("accounts") or {}
    return ("网关状态：\n"
            "  · 版本：%s\n"
            "  · 状态：%s\n"
            "  · 账号池：%s 个（可用 %s）\n"
            "  · 模型：%s 个" % (
                h.get("version", "?"), h.get("status", "?"),
                acc.get("total", "?"), acc.get("usable", "?"),
                h.get("models_count", "?")))


def _tool_task(_args: Dict[str, Any]) -> str:
    d = _http_json(WEBUI_BASE + "/api/wb-daily/jobs") or {}
    jobs = d.get("jobs") or []
    if not jobs:
        return "暂无任务记录"
    j = jobs[0]
    return ("每日任务：\n"
            "  · 最近一轮：%s\n"
            "  · 状态：%s\n"
            "  · 进度：%s（账号 %s/%s）" % (
                j.get("job_id"), j.get("status"), j.get("progress"),
                j.get("accounts_done"), j.get("accounts_total")))


def _tool_reqstats(_args: Dict[str, Any]) -> str:
    d = _http_json(WEBUI_BASE + "/api/reqstats?limit=50") or {}
    if not d:
        return "暂无请求指标"
    parts = ["请求指标："]
    for k in ("total", "success", "failed", "successRate", "avgMs", "p95Ms"):
        if k in d:
            parts.append("  · %s：%s" % (k, d.get(k)))
    recent = d.get("recent") or []
    if recent:
        parts.append("  · 最近 %d 条请求" % len(recent))
    return "\n".join(parts)


# 注册表：name -> (函数, 描述, 参数说明)
_TOOLS: Dict[str, tuple] = {
    "get_credits": (_tool_credits, "积分统计（剩余/今日/近7天/本月）", "无参数"),
    "get_accounts": (_tool_accounts, "账号余额明细", "limit: 返回账号数，默认 23"),
    "get_models": (_tool_models, "当前模型清单与禁用分类", "无参数"),
    "get_status": (_tool_status, "网关与账号池状态", "无参数"),
    "get_task_progress": (_tool_task, "每日任务进度", "无参数"),
    "get_request_stats": (_tool_reqstats, "请求指标（成功率/耗时）", "无参数"),
}


def _tool_registry() -> List[Dict[str, Any]]:
    """工具声明（OpenAI function calling 格式）。"""
    props = {
        "get_credits": {"type": "object", "properties": {}},
        "get_accounts": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回账号数，默认 23"}}},
        "get_models": {"type": "object", "properties": {}},
        "get_status": {"type": "object", "properties": {}},
        "get_task_progress": {"type": "object", "properties": {}},
        "get_request_stats": {"type": "object", "properties": {}},
    }
    return [{"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": props.get(name) or {"type": "object", "properties": {}},
    }} for name, (_fn, desc, _p) in _TOOLS.items()]


def _dispatch_tool(name: str, args: Dict[str, Any]) -> str:
    """执行工具；任何异常都转成可读文本（不让 LLM 看到堆栈）。"""
    entry = _TOOLS.get(str(name or ""))
    if not entry:
        return "工具不存在：%s" % name
    fn = entry[0]
    try:
        return str(fn(args if isinstance(args, dict) else {}))
    except Exception as e:
        return "查询失败（%s）：%s" % (type(e).__name__, str(e)[:100])


# ---------------------------------------------------------------------------
# 模型选择
# ---------------------------------------------------------------------------

def _pick_model() -> Optional[str]:
    if ASSISTANT_MODEL:
        return ASSISTANT_MODEL
    try:
        d = _http_json(GATEWAY_BASE + "/v1/models", timeout=6.0) or {}
        ids = [str(m.get("id")) for m in (d.get("data") or []) if m.get("id")]
        for want in _FALLBACK_MODELS:
            if want in ids:
                return want
        return ids[0] if ids else None
    except Exception:
        return _FALLBACK_MODELS[0]


# ---------------------------------------------------------------------------
# 核心：一次助手问答（带一轮工具调用）
# ---------------------------------------------------------------------------

# 模型把工具调用「原文回吐」时的特征（hy3 实测：第二轮不带 tools 时，
# 它会把 `<tool_calls:xxx>` / `<tool_call:xxx>get_status` 当成正文输出）。
# 这种输出对用户毫无意义，必须识别出来并回落到工具结果本身。
import re as _re
_TOOL_ECHO_RX = _re.compile(r"^\s*<\s*tool_call[s]?\b", _re.I)


def _is_tool_echo(text: str) -> bool:
    """判断是否是工具调用的原文回吐（而非正常回答）。"""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_TOOL_ECHO_RX.match(t))


def assistant_reply(session_id: str, question: str) -> str:
    """自然语言 → （可选工具调用）→ 中文回答。

    返回空字符串表示助手不可用（调用方应回落到命令兜底文案）。
    """
    if not ASSISTANT_ENABLED:
        return ""
    q = (question or "").strip()
    if not q:
        return ""
    model = _pick_model()
    if not model:
        return ""

    messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(_history(session_id))
    messages.append({"role": "user", "content": q[:1500]})

    tools = _tool_registry()
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    try:
        import urllib.request
        req = urllib.request.Request(
            GATEWAY_BASE + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=ASSISTANT_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return ""

    try:
        msg = (d.get("choices") or [{}])[0].get("message") or {}
    except Exception:
        return ""

    # 一轮工具调用：把结果喂回去，再取最终回答
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": tool_calls})
        for tc in tool_calls[:3]:                      # 上限 3 个，防刷
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                args = {}
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or name,
                "content": _dispatch_tool(name, args)[:MAX_OUTPUT_CHARS],
            })
        try:
            payload2 = dict(payload)
            payload2["messages"] = messages
            # 第二轮：保留 tools 声明但要求**必须出正文**（tool_choice=none）。
            # 此前是直接删掉 tools，hy3 会因此把工具调用原文当正文回吐
            # （实测输出 `<tool_calls:xxx>`，对用户毫无意义）。
            payload2["tool_choice"] = "none"
            final = ""
            try:
                req2 = urllib.request.Request(
                    GATEWAY_BASE + "/v1/chat/completions",
                    data=json.dumps(payload2).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req2, timeout=ASSISTANT_TIMEOUT) as r2:
                    d2 = json.loads(r2.read().decode("utf-8", "replace"))
                msg2 = (d2.get("choices") or [{}])[0].get("message") or {}
                # 模型可能又发起工具调用（或不支持 tool_choice=none）——
                # 两种情况下都不采纳它的正文，直接走工具结果回落。
                if not (msg2.get("tool_calls") or []):
                    final = (msg2.get("content") or "").strip()
            except Exception:
                # tool_choice=none 不被支持时，退回「不带 tools」再试一次。
                try:
                    payload2.pop("tools", None)
                    payload2.pop("tool_choice", None)
                    req2 = urllib.request.Request(
                        GATEWAY_BASE + "/v1/chat/completions",
                        data=json.dumps(payload2).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req2, timeout=ASSISTANT_TIMEOUT) as r2:
                        d2 = json.loads(r2.read().decode("utf-8", "replace"))
                    msg2 = (d2.get("choices") or [{}])[0].get("message") or {}
                    if not (msg2.get("tool_calls") or []):
                        final = (msg2.get("content") or "").strip()
                except Exception:
                    final = ""
            # 回吐工具调用原文 → 视为无效输出
            if _is_tool_echo(final):
                final = ""
        except Exception:
            final = ""
        if final:
            _remember(session_id, "user", q)
            _remember(session_id, "assistant", final)
            return final[:MAX_OUTPUT_CHARS]
        # 第二轮没拿到可用正文：至少把工具结果直接给用户（比报错有用）。
        tool_text = "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "tool")
        if tool_text:
            return tool_text[:MAX_OUTPUT_CHARS]
        return ""

    content = msg.get("content") or ""
    if not content.strip():
        return ""
    _remember(session_id, "user", q)
    _remember(session_id, "assistant", content)
    return content.strip()[:MAX_OUTPUT_CHARS]


def is_command(text: str) -> bool:
    """判断是否为已知命令（命令优先，不进助手）。

    与 `wecom_command_reply` 的命令分支保持同一套关键词，
    否则会出现「这里判成非命令（进 LLM）、那边又命中命令」的分裂。
    """
    t = (text or "").strip().lower().lstrip("/").split("@", 1)[0].strip()
    if not t:
        return False
    if t in ("start", "notify_on", "notify_off", "help"):
        return True
    _EN2ZH = {"status": "状态", "credits": "积分", "balance": "积分",
              "accounts": "账号", "models": "模型",
              "daily": "任务", "run": "任务", "checkin": "签到"}
    if t in _EN2ZH:
        return True
    for kw in ("帮助", "菜单", "通知", "账号", "模型", "积分",
               "余额", "状态", "签到", "任务"):
        if kw in t:
            return True
    return False
