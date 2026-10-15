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
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

ASSISTANT_ENABLED = (os.getenv("AB_ASSISTANT_ENABLED", "1") or "1").strip() not in ("0", "false", "no")
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

# ---------------------------------------------------------------------------
# Provider 注册表（参考 MoviePilot 的 LLMProviderInfo / ProviderSpec）
#
# 用户需求（2026-10-14 第三阶段）：「在设置页面添加智能助手模型配置，
# 而不是你自己写，参考 MoviePilot 的相关配置，默认自带模型提供，
# 也可以配置其他模型」。
#
# 设计对齐 MoviePilot `app/schemas/llm.py` 的 LLMProviderInfo：
#   - `base_url_presets`：预设地址（下拉选，省得手打）
#   - `base_url_editable`：是否允许自定义地址
#   - `requires_base_url`：是否必须填地址
#   - `supports_api_key`：是否需要密钥；secret 语义与通知渠道一致
#     （GET 不回明文、空串=清除、不提交=保留）
#   - `supports_model_refresh`：能否拉 /v1/models 刷新模型清单
#   - `description`：设置页上的说明
#
# 与 MoviePilot 的差异（场景不同，不照搬）：
#   - 没有 OAuth 授权流（我们是自托管网关，密钥模式足够）
#   - 不做「服务端工具能力」探测（那是给 OpenAI/Anthropic 官方端的能力，
#     我们走的是账号池中转，工具调用由 assistant 自己实现）
# ---------------------------------------------------------------------------

# 配置载体：独立文件，避免和通知配置耦在一起（两者生命周期不同）。
CONFIG_FILE = Path(os.getenv("AB_ASSISTANT_CONFIG",
                              str(Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "assistant_config.json")))

# ⚠️ 必须用 RLock 而不是 Lock：`_save_config` 持有锁期间会调用
# `_load_config()`（读-改-写需要基于最新值），普通 Lock 不可重入会自死锁。
# 2026-10-14 实测踩过：本地测试跑到 _save_config 直接挂住，整条命令无输出。
_CONFIG_LOCK = threading.RLock()


def _default_provider() -> str:
    """默认 provider id。env 可覆盖（AB_ASSISTANT_PROVIDER）。"""
    return (os.getenv("AB_ASSISTANT_PROVIDER", "") or "").strip() or "gateway"


# 注册表：key -> 定义。新 provider 往这里加即可，前端自动渲染。
PROVIDERS: List[Dict[str, Any]] = [
    {
        "key": "gateway",
        "name": "AutoBuddy 网关（自带）",
        "desc": "复用本网关的账号池与模型目录，无需额外密钥。"
                "默认选项，开箱即用。",
        "default_base_url": GATEWAY_BASE,
        "base_url_presets": [
            {"id": "local", "label": "本机网关（默认）", "value": GATEWAY_BASE},
        ],
        "base_url_editable": True,
        "requires_base_url": True,
        "supports_api_key": False,
        "supports_model_refresh": True,
        "models": [],                      # 空 = 运行时从 /v1/models 动态拉取
    },
    {
        "key": "openai",
        "name": "OpenAI 兼容接口",
        "desc": "任意 OpenAI 兼容的 /v1/chat/completions 端点"
                "（OpenAI、DeepSeek、Moonshot、OneAPI、New API 等）。",
        "default_base_url": "https://api.openai.com/v1",
        "base_url_presets": [
            {"id": "openai", "label": "OpenAI 官方", "value": "https://api.openai.com/v1"},
            {"id": "deepseek", "label": "DeepSeek", "value": "https://api.deepseek.com/v1"},
            {"id": "moonshot", "label": "Moonshot（Kimi）", "value": "https://api.moonshot.cn/v1"},
            {"id": "siliconflow", "label": "硅基流动", "value": "https://api.siliconflow.cn/v1"},
        ],
        "base_url_editable": True,
        "requires_base_url": True,
        "supports_api_key": True,
        "api_key_label": "API Key",
        "api_key_hint": "留空且已配置过 = 保持不变；填值 = 覆盖；明确提交空串 = 清除",
        "supports_model_refresh": True,
        "models": [],
    },
]

_PROVIDER_BY_KEY = {p["key"]: p for p in PROVIDERS}


def _blank_config() -> Dict[str, Any]:
    """配置默认值。"""
    return {
        "enabled": True,
        "provider": _default_provider(),
        "model": "",                      # 空 = 自动挑选
        "systemPrompt": "",               # 空 = 用内置 SYSTEM_PROMPT
        "providers": {
            "gateway": {"base_url": GATEWAY_BASE, "api_key": ""},
            "openai": {"base_url": "https://api.openai.com/v1", "api_key": ""},
        },
    }


def _load_config() -> Dict[str, Any]:
    """读取配置（不存在/损坏 → 默认值）。"""
    with _CONFIG_LOCK:
        cfg = _blank_config()
        try:
            if CONFIG_FILE.exists():
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg.update({k: v for k, v in raw.items() if k in cfg})
                    provs = raw.get("providers")
                    if isinstance(provs, dict):
                        for k, v in provs.items():
                            if k in cfg["providers"] and isinstance(v, dict):
                                cfg["providers"][k].update(v)
        except Exception:
            pass
        return cfg


def _save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并保存；secret 语义：不提交=保留、空串=清除、有值=覆盖。"""
    with _CONFIG_LOCK:
        cfg = _load_config()
        body = patch or {}
        for k in ("enabled", "provider", "model", "systemPrompt"):
            if k in body:
                cfg[k] = body[k]
        provs = body.get("providers")
        if isinstance(provs, dict):
            for pk, pv in provs.items():
                if pk not in cfg["providers"] or not isinstance(pv, dict):
                    continue
                slot = cfg["providers"][pk]
                for k in ("base_url", "api_key"):
                    if k not in pv:
                        continue          # 未提交 → 保留
                    v = pv[k]
                    if v is None:
                        continue          # 保留
                    slot[k] = str(v or "").strip()
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        except Exception:
            pass
        return cfg


def _runtime() -> Dict[str, Any]:
    """当前生效的运行时配置（provider + base_url + key + model）。"""
    cfg = _load_config()
    if not cfg.get("enabled", True):
        return {"enabled": False}
    pk = str(cfg.get("provider") or _default_provider())
    prov = _PROVIDER_BY_KEY.get(pk) or _PROVIDER_BY_KEY[_default_provider()]
    slot = (cfg.get("providers") or {}).get(prov["key"]) or {}
    base = str(slot.get("base_url") or "").strip() or prov.get("default_base_url") or ""
    return {
        "enabled": True,
        "provider": prov["key"],
        "base_url": base.rstrip("/"),
        "api_key": str(slot.get("api_key") or "").strip(),
        "model": str(cfg.get("model") or "").strip(),
        "systemPrompt": str(cfg.get("systemPrompt") or "").strip(),
    }


def public_view() -> Dict[str, Any]:
    """设置页视图：注册表 + 当前配置（密钥只回 has_value，不回明文）。"""
    cfg = _load_config()
    provs_out: Dict[str, Any] = {}
    for p in PROVIDERS:
        slot = (cfg.get("providers") or {}).get(p["key"]) or {}
        provs_out[p["key"]] = {
            "base_url": str(slot.get("base_url") or p.get("default_base_url") or ""),
            "api_key": {"has_value": bool(str(slot.get("api_key") or "").strip())},
        }
    return {
        "registry": PROVIDERS,
        "config": {
            "enabled": bool(cfg.get("enabled", True)),
            "provider": str(cfg.get("provider") or _default_provider()),
            "model": str(cfg.get("model") or ""),
            "systemPrompt": str(cfg.get("systemPrompt") or ""),
            "providers": provs_out,
        },
        "runtime": session_stats(),
    }

SYSTEM_PROMPT = """你是 AutoBuddy 网关的智能助手「小助手」，运行在企微/Telegram 交互里。

## 你是谁

你是这个网关的「智能管家」—— 不是命令解释器，也不是查询表单。你有自己的身份：
熟悉这个网关的一切（账号、积分、模型、任务、巡检），能查、能判断、也能在用户
确认后代为操作。说话自然、像同事，不像机器。

## 怎么说话

- 用**中文**，口语化一点。可以叫用户「哥哥」或直说，别用「您」堆敬语。
- 被问「你能做什么 / 你可以做什么」时，**不要罗列功能清单**。用一两句话介绍
  自己是谁、能帮上什么，再自然带出两三个例子。例如：
  「我是 AutoBuddy 的智能助手，帮你盯着网关的积分、账号和任务。你可以问我
  现在积分还剩多少、哪个账号快过期了、今天的任务跑完没有 —— 也可以直接让我
  执行任务或签到，我会先跟你确认一遍再动手。」
- 回答基于工具返回的真实数据，**严格依据工具结果，不要编造数字**。
  工具失败或为空时，如实说「这个暂时查不到」，别猜。
- 数据之外可以给一句简短解读（如「余额偏低，建议关注」「这个账号 3 天后过期」），
  让回答有判断力，不只是复读。
- **简洁**：一般不超过 6 行；用户要明细时再展开。
- 不要输出 Markdown 标题、代码块；用 plain text + 短横线列表。

## 执行操作（启动任务/签到/中止任务/巡检/开关通知）

这些操作**由你代为执行**，不是让用户自己去敲命令。用户说「执行任务」，
系统会走确认流程（返回一句【待确认】+ 影响范围 + 预计耗时），用户回「确认」
才真正执行。你**不需要**自己判断能不能执行，也不要说「请用命令」——
确认流程会处理好安全性。

若你看到【待确认】样式的回复，说明已在确认中，正常转述即可。

## 边界

- 不知道就说不知道，不要装懂。
- 不执行登录/改密/删除数据这类高危操作 —— 那些请在设置页人工完成。"""


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
    """运行态摘要（供「助手」命令与设置页展示）。"""
    rt = _runtime()
    with _SESSIONS_LOCK:
        return {"enabled": bool(rt.get("enabled")),
                "sessions": len(_SESSIONS),
                "provider": rt.get("provider") or "",
                "model": rt.get("model") or "(自动挑选)",
                "ttlSec": SESSION_TTL}


# ---------------------------------------------------------------------------
# 工具集（只读）
# ---------------------------------------------------------------------------

def _http_json(url: str, timeout: float = 8.0, method: str = "GET",
               body: Optional[bytes] = None,
               api_key: str = "") -> Any:
    """同步取 JSON。``api_key`` 非空时带 Authorization 头（自定义 provider 用）。"""
    import urllib.request
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", "Bearer %s" % api_key)
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
    """每日任务进度 + **今日执行历史**。

    2026-10-14 用户反馈「我只是问他今天执行过任务吗？为何就直接执行了」——
    除了路由缺陷，还暴露本工具**答不了历史问题**：原先只读内存态
    `/api/wb-daily/jobs`（只有当前进程的最近一轮，容器重启即丢），
    「今天跑过吗」根本无从判断。
    现补 `runs`（SQLite 落库历史），并统计今日轮次。
    """
    import datetime as _dt
    import time as _t
    today = _dt.date.today().isoformat()

    # 1) 今日历史（落库，跨重启）
    runs = []
    try:
        d = _http_json(WEBUI_BASE + "/api/wb-daily/runs?limit=50") or {}
        runs = d if isinstance(d, list) else (d.get("runs") or d.get("items") or [])
    except Exception:
        runs = []

    def _day(ts):
        try:
            return _dt.date.fromtimestamp(float(ts)).isoformat()
        except Exception:
            return ""

    today_runs = [r for r in runs if _day(r.get("started_at")) == today]
    done_today = [r for r in today_runs if str(r.get("status")) == "done"]

    # 2) 当前/最近一轮（内存态，含实时进度）
    jobs = []
    try:
        d2 = _http_json(WEBUI_BASE + "/api/wb-daily/jobs") or {}
        jobs = d2.get("jobs") or []
    except Exception:
        jobs = []

    lines = ["每日任务："]
    if not today_runs:
        lines.append("  · 今天（%s）**没有执行记录**。" % today)
    else:
        lines.append("  · 今天执行了 %d 轮（成功 %d 轮）。"
                     % (len(today_runs), len(done_today)))
        for r in today_runs[-5:]:
            st = r.get("started_at")
            try:
                hm = _dt.datetime.fromtimestamp(float(st)).strftime("%H:%M")
            except Exception:
                hm = "?"
            fin = r.get("finished_at")
            dur = ""
            try:
                if st and fin:
                    dur = "（%.0f 分钟）" % ((float(fin) - float(st)) / 60.0)
            except Exception:
                dur = ""
            lines.append("    - %s 启动 · %s%s · 账号 %s" % (
                hm, r.get("status"), dur, r.get("accounts") or "?"))

    if jobs:
        j = jobs[0]
        lines.append("  · 最近一轮：%s · %s · 账号 %s/%s" % (
            j.get("status"), j.get("progress"),
            j.get("accounts_done"), j.get("accounts_total")))
    return "\n".join(lines)


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


# ---------------------------------------------------------------------------
# 阶段 4 新增工具：查询能力全覆盖（2026-10-14）
#
# 用户确认按三阶段路线推进，本阶段把只读查询从 6 个扩到 15 个，
# 覆盖 AutoBuddy 五大功能域（积分/账号池/模型/每日任务/网关密钥）。
#
# 全部只读；写操作留给阶段 5（两段式确认）。
# ---------------------------------------------------------------------------

def _tool_account_pool(_args: Dict[str, Any]) -> str:
    """账号池概览：模式、启用/停用数、最近选号。"""
    d = _http_json(WEBUI_BASE + "/api/account-pool") or {}
    if not d:
        return "账号池数据获取失败"
    accs = d.get("accounts") or []
    enabled = [a for a in accs if a.get("enabled")]
    disabled = [a for a in accs if a.get("disabled")]
    usable = [a for a in accs if a.get("usable")]
    mode_map = {"auto": "自动选号", "manual": "固定账号"}
    lines = ["账号池：",
             "  · 模式：%s" % mode_map.get(str(d.get("mode")), d.get("mode")),
             "  · 账号总数：%d（启用 %d / 停用 %d）" % (len(accs), len(enabled), len(disabled)),
             "  · 可用（凭据有效）：%d" % len(usable)]
    if d.get("lastSelectedSource"):
        lines.append("  · 最近选号来源：%s" % d.get("lastSelectedSource"))
    if d.get("manualAccountId"):
        lines.append("  · 固定账号：%s" % d.get("manualAccountId"))
    if disabled:
        names = ", ".join(str(a.get("name"))[:12] for a in disabled[:8])
        lines.append("  · 已停用：%s%s" % (names, " 等 %d 个" % len(disabled) if len(disabled) > 8 else ""))
    return "\n".join(lines)


def _tool_selection_stats(args: Dict[str, Any]) -> str:
    """选号分布：谁承接多、当前在飞数。"""
    limit = int(args.get("limit") or 10)
    d = _http_json(WEBUI_BASE + "/api/account-pool/selections?limit=200") or {}
    counts = d.get("counts") or []
    inflight = d.get("inflight") or {}
    if not counts:
        return "暂无选号记录"
    total = d.get("total") or sum(c.get("count", 0) for c in counts)
    lines = ["选号分布（近 %d 次，涉及 %s 个账号）：" % (total, d.get("distinctAccounts") or len(counts))]
    for c in counts[:max(1, min(limit, 20))]:
        name = str(c.get("accountName") or c.get("accountId") or "?")
        n = c.get("count") or 0
        pct = (n * 100.0 / total) if total else 0
        lines.append("  %s: %d 次（%.1f%%）" % (name[:16], n, pct))
    busy = {k: v for k, v in inflight.items() if v}
    if busy:
        lines.append("  · 当前在飞请求：%d 个账号" % len(busy))
    else:
        lines.append("  · 当前无在飞请求")
    return "\n".join(lines)


def _tool_disabled_models(_args: Dict[str, Any]) -> str:
    """模型禁用明细：按账号列出被禁用的模型。

    数据源是 `/api/account-pool` 的 `modelPolicy`（`model_policy.as_model_lists`
    的投影：账号 -> **被禁用**的模型名列表）。

    ⚠️ 2026-10-14 修正：原先读 `/api/account-models` 的 `accounts` 字段，但那里
    是 dict（键为账号 ID）且 `models` 是**可用全集**（实测 38 个 = catalog 全集），
    于是差集恒为 0，工具输出「没有任何禁用」而 `disabledTotal` 却是 354 ——
    口径完全对不上。现改读权威禁用清单。
    """
    d = _http_json(WEBUI_BASE + "/api/account-pool") or {}
    pol = d.get("modelPolicy") or {}
    if not pol:
        return "当前没有任何账号×模型被禁用"
    # 名字映射：账号 ID -> 可读名称
    name_by_id = {}
    for a in (d.get("accounts") or []):
        aid = a.get("id")
        if aid:
            name_by_id[str(aid)] = str(a.get("name") or a.get("nickname") or aid)
    total = sum(len(v or []) for v in pol.values())
    lines = ["模型禁用情况（%d 个账号、共 %d 个组合被禁用）：" % (len(pol), total)]
    # 禁用多的排前面，便于快速定位重点账号
    rows = sorted(pol.items(), key=lambda kv: -len(kv[1] or []))
    for aid, models in rows[:12]:
        name = name_by_id.get(str(aid), str(aid)[:16])
        ms = [str(x) for x in (models or [])]
        lines.append("  %s：%d 个 — %s%s" % (
            name[:16], len(ms), ", ".join(m[:16] for m in ms[:8]),
            " …等 %d 个" % len(ms) if len(ms) > 8 else ""))
    if len(rows) > 12:
        lines.append("  …及其他 %d 个账号" % (len(rows) - 12))
    return "\n".join(lines)


def _tool_health_check(_args: Dict[str, Any]) -> str:
    """巡检配置与覆盖范围。"""
    d = _http_json(WEBUI_BASE + "/api/model-health") or {}
    cfg = d.get("config") or {}
    accs = d.get("accounts") or []
    if not cfg:
        return "巡检配置获取失败"
    lines = ["模型可用性巡检：",
             "  · 自动巡检：%s" % ("开启" if cfg.get("enabled") else "关闭"),
             "  · 间隔：%s 分钟" % cfg.get("intervalMinutes"),
             "  · 只测常用模型：%s" % ("是" if cfg.get("onlyUsedModels") else "否"),
             "  · 不可用自动禁用：%s" % ("是" if cfg.get("autoDisableAccounts") else "否"),
             "  · 覆盖账号：%d 个" % len(accs)]
    return "\n".join(lines)


def _tool_account_credentials(_args: Dict[str, Any]) -> str:
    """账号凭据有效期：哪些号快过期。"""
    d = _http_json(WEBUI_BASE + "/api/wb-daily/accounts") or {}
    accs = d.get("accounts") or []
    if not accs:
        return "账号凭据数据获取失败"
    import time as _t
    now = _t.time()
    rows = []
    for a in accs:
        exp = a.get("exp")
        if not exp:
            continue
        left_days = (float(exp) - now) / 86400.0
        rows.append((left_days, str(a.get("name") or a.get("user") or "?"),
                     bool(a.get("has_at")), bool(a.get("has_rt"))))
    if not rows:
        return "未获取到凭据有效期"
    rows.sort()
    lines = ["账号凭据有效期（共 %d 个）：" % len(rows)]
    urgent = [r for r in rows if r[0] < 7]
    for left, name, has_at, has_rt in rows[:10]:
        if left < 0:
            state = "已过期"
        elif left < 7:
            state = "%.1f 天后过期 ⚠️" % left
        else:
            state = "%.0f 天" % left
        cred = ("AT+RT" if (has_at and has_rt) else ("仅AT" if has_at else "无"))
        lines.append("  %s: %s（%s）" % (name[:16], state, cred))
    if urgent:
        lines.append("  ⚠️ %d 个账号 7 天内到期，建议及时续期" % len(urgent))
    return "\n".join(lines)


def _tool_task_ledger(args: Dict[str, Any]) -> str:
    """任务台账：各成长任务的完成情况。"""
    limit = int(args.get("limit") or 10)
    d = _http_json(WEBUI_BASE + "/api/wb-daily/task-summary?limit=60") or {}
    items = d.get("items") or []
    if not items:
        return "暂无任务台账"
    items = sorted(items, key=lambda x: -(x.get("total") or 0))
    lines = ["任务台账（Top %d，按累计次数）：" % min(limit, len(items))]
    for it in items[:max(1, min(limit, 20))]:
        name = str(it.get("task_name") or it.get("task_key") or "?")
        grp = str(it.get("group_name") or "")
        tot = it.get("total") or 0
        ok = it.get("success") or 0
        rate = (ok * 100.0 / tot) if tot else 0
        lines.append("  %s%s：%d 次，成功 %d（%.0f%%）" % (
            name[:18], ("［%s］" % grp[:8]) if grp else "", tot, ok, rate))
    return "\n".join(lines)


def _tool_credit_breakdown(_args: Dict[str, Any]) -> str:
    """积分明细：今日签到/任务/累计，按天趋势。"""
    d = _http_json(WEBUI_BASE + "/api/wb-daily/credit-summary") or {}
    if not d:
        return "积分明细获取失败"
    days = d.get("days") or []
    lines = ["积分明细：",
             "  · 今日合计：%s" % _fmt_num(d.get("todayCredits")),
             "    - 签到：%s" % _fmt_num(d.get("todayCheckinCredits")),
             "    - 任务：%s" % _fmt_num(d.get("todayTaskCredits")),
             "  · 累计（台账）：%s" % _fmt_num(d.get("totalCredits"))]
    if days:
        lines.append("  · 近 %d 天：" % min(len(days), 7))
        for dd in days[:7]:
            lines.append("    - %s：合计 %s（签到 %s / 任务 %s）" % (
                dd.get("date"), _fmt_num(dd.get("total")),
                _fmt_num(dd.get("checkin")), _fmt_num(dd.get("task"))))
    return "\n".join(lines)


def _tool_api_keys(_args: Dict[str, Any]) -> str:
    """API Key 状态：数量、启用、调用量。"""
    d = _http_json(WEBUI_BASE + "/api/api-keys") or {}
    keys = d.get("keys") or []
    if not keys:
        return "暂无 API Key（当前为开放模式）" if not d.get("requireKey") else "暂无 API Key"
    lines = ["API Key（%d 个，%s）：" % (
        len(keys), "需要密钥" if d.get("requireKey") else "开放模式")]
    for k in keys[:10]:
        lines.append("  %s：%s · 调用 %s 次" % (
            str(k.get("name") or k.get("id") or "?")[:16],
            "启用" if k.get("enabled") else "停用",
            k.get("callCount") or 0))
    return "\n".join(lines)


def _tool_notify_status(_args: Dict[str, Any]) -> str:
    """通知渠道与场景状态。"""
    try:
        try:
            from gateway import notify as _nt
        except ImportError:
            import notify as _nt  # type: ignore
        v = _nt.public_view()
    except Exception as e:
        return "通知状态获取失败：%s" % str(e)[:80]
    chans = v.get("channels") or {}
    scenes = v.get("scenes") or []
    lines = ["通知：",
             "  · 总开关：%s" % ("开启" if v.get("enabled") else "关闭")]
    for key, view in chans.items():
        name = view.get("name") or key
        on = view.get("_enabled", True)
        ready = view.get("_ready")
        state = "已启用" if on else "已停用"
        if not ready:
            state += "（未配置完整）"
        lines.append("  · %s：%s" % (name, state))
    if scenes:
        enabled_scenes = [sc.get("name") for sc in scenes if sc.get("enabled", True)]
        lines.append("  · 消息场景：%d 个（%s）" % (
            len(scenes), ", ".join(str(x) for x in enabled_scenes[:6]) or "无"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 阶段 6 新增工具：主动智能（2026-10-14）
#
# 用户确认的三阶段路线最后一段：「异常归因、趋势判断、主动播报」。
# 本阶段先落地前两个**只读**能力（主动播报接通知场景，需另行设计去重）。
#
# 与阶段 4 工具的区别：这两个不只是「取数」，而是**基于数据做判断并给建议** ——
# 这才是 LLM 之外、代码层就该完成的「智能」。
# ---------------------------------------------------------------------------

def _tool_credit_trend(_args: Dict[str, Any]) -> str:
    """积分趋势：按近期消耗速度估算还能撑几天，并给建议。

    口径说明：
      - 日均消耗取「近 7 天 / 7」（比今日单日更稳，避免某天没用量造成误判）；
      - 同时给「本月日均」作对照，两个口径差异大时提醒用户留意节奏变化；
      - 余额低于 7 天用量时明确预警。
    """
    d = _http_json(WEBUI_BASE + "/api/credits/stats") or {}
    s = d.get("summary") or {}
    if not s:
        return "积分数据获取失败"
    remaining = s.get("currentRemaining") or 0
    cap = s.get("currentCapacity") or 0
    u7 = s.get("usage7Days") or 0
    um = s.get("usageThisMonth") or 0
    ut = s.get("usageToday") or 0

    try:
        remaining = float(remaining); u7 = float(u7)
        um = float(um); ut = float(ut); cap = float(cap)
    except (TypeError, ValueError):
        return "积分数据格式异常"

    lines = ["积分趋势："]
    lines.append("  · 当前剩余：%s（总额度 %s，已用 %.1f%%）" % (
        _fmt_num(remaining), _fmt_num(cap),
        (cap - remaining) * 100.0 / cap if cap else 0.0))

    # 近 7 天日均（主口径）
    d7 = u7 / 7.0
    # 本月日均（对照口径）
    import datetime as _dt2
    day_now = _dt2.date.today().day
    dm = um / float(day_now) if day_now else 0.0

    lines.append("  · 近 7 天日均消耗：%s" % _fmt_num(d7))
    if day_now > 1 and dm > 0:
        lines.append("  · 本月日均消耗：%s（已过 %d 天）" % (_fmt_num(dm), day_now))

    if d7 <= 0:
        lines.append("  · 近 7 天无消耗，暂无法估算可用天数。")
    else:
        days = remaining / d7
        # 预期耗尽日期
        try:
            eta = _dt2.date.today() + _dt2.timedelta(days=int(days))
            eta_txt = eta.isoformat()
        except Exception:
            eta_txt = "?"
        lines.append("  · 按近 7 天速度，约可支撑 **%.1f 天**（预计 %s 前后见底）"
                     % (days, eta_txt))
        if days < 7:
            lines.append("  ⚠️ 不足 7 天！建议减少调用或补充账号额度。")
        elif days < 15:
            lines.append("  · 余量偏紧，建议关注。")

    # 节奏对比：今日 vs 近 7 天日均
    if d7 > 0 and ut > d7 * 1.5:
        lines.append("  · 今日消耗 %s 明显高于近期日均，注意是否有异常调用。"
                     % _fmt_num(ut))
    return "\n".join(lines)


def _tool_diagnose_task(args: Dict[str, Any]) -> str:
    """异常归因：分析最近的失败任务，给出原因与建议。

    数据来源：`/api/wb-daily/runs` 的 `summary.detail`（v0.9.61 起落库的
    真实失败原因）+ 逐账号明细（账号级失败）。
    """
    limit = int(args.get("limit") or 5)
    runs = []
    try:
        d = _http_json(WEBUI_BASE + "/api/wb-daily/runs?limit=30") or {}
        runs = d if isinstance(d, list) else (d.get("runs") or d.get("items") or [])
    except Exception:
        runs = []
    # ⚠️ 2026-10-14 修正：原判据 `status != "done"` 把 **running** 也算成失败，
    # 导致「正在跑的任务」被报成异常（线上实测误报 9789ee6e3782）。
    # 只有**终态且非成功**才算失败：failed / aborted。
    _OK_OR_RUNNING = ("done", "running", "pending")
    failed = [r for r in runs
              if str(r.get("status") or "") not in _OK_OR_RUNNING]
    if not failed:
        return "最近 %d 轮任务全部成功，无需归因。" % min(limit, len(runs) or 1)

    lines = ["任务异常归因（最近 %d 轮中有 %d 轮未成功）："
             % (min(len(runs), 30), len(failed))]
    for r in failed[:max(1, min(limit, 5))]:
        jid = str(r.get("job_id") or "?")[:12]
        mode = r.get("mode") or "?"
        started = r.get("started_at")
        try:
            import datetime as _dt3
            hm = _dt3.datetime.fromtimestamp(float(started)).strftime("%m-%d %H:%M")
        except Exception:
            hm = "?"
        s = r.get("summary") or {}
        detail = ""
        if isinstance(s, dict):
            detail = str(s.get("detail") or "")
        # 与主动播报**共用同一套映射**（notify._classify_task_failure），
        # 保证「问助手」和「主动推送」口径一致，不会出现两套说法。
        try:
            try:
                from gateway import notify as _nt6
            except ImportError:
                import notify as _nt6  # type: ignore
            reason, advice = _nt6._classify_task_failure(detail)
        except Exception:
            reason, advice = (detail[:80] or "原因未记录"), "可查看容器日志进一步定位"
        lines.append("  · %s（%s，%s）" % (jid, hm, mode))
        lines.append("    - 原因：%s" % reason)
        lines.append("    - 建议：%s" % advice)
    return "\n".join(lines)


# 注册表：name -> (函数, 描述, 参数说明)
_TOOLS: Dict[str, tuple] = {
    # ---- 原有 6 个 ----
    "get_credits": (_tool_credits, "积分统计（剩余/今日/近7天/本月）", "无参数"),
    "get_accounts": (_tool_accounts, "账号余额明细", "limit: 返回账号数，默认 23"),
    "get_models": (_tool_models, "当前模型清单与禁用分类", "无参数"),
    "get_status": (_tool_status, "网关与账号池状态", "无参数"),
    "get_task_progress": (_tool_task, "每日任务进度与今日执行历史", "无参数"),
    "get_request_stats": (_tool_reqstats, "请求指标（成功率/耗时）", "无参数"),
    # ---- 阶段 4 新增 9 个（查询全覆盖）----
    "get_account_pool": (_tool_account_pool, "账号池概览：选号模式、启用/停用账号数、最近选号来源", "无参数"),
    "get_selection_stats": (_tool_selection_stats, "选号分布：各账号承接次数与占比、当前在飞请求", "limit: 列出账号数，默认 10"),
    "get_disabled_models": (_tool_disabled_models, "模型禁用明细：哪些账号禁用了哪些模型", "无参数"),
    "get_health_check": (_tool_health_check, "模型可用性巡检：开关、间隔、覆盖账号数", "无参数"),
    "get_account_credentials": (_tool_account_credentials, "账号凭据有效期：哪些账号快过期需续期", "无参数"),
    "get_task_ledger": (_tool_task_ledger, "任务台账：各成长任务的累计次数与成功率", "limit: 列出任务数，默认 10"),
    "get_credit_breakdown": (_tool_credit_breakdown, "积分明细：今日签到/任务/累计，近 7 天按天趋势", "无参数"),
    "get_api_keys": (_tool_api_keys, "API Key 状态：数量、启停、调用次数", "无参数"),
    "get_notify_status": (_tool_notify_status, "通知渠道与消息场景状态", "无参数"),
    # ---- 阶段 6 新增（主动智能）----
    "get_credit_trend": (_tool_credit_trend, "积分趋势：按近期消耗估算可支撑天数、预警见底风险", "无参数"),
    "diagnose_task_failure": (_tool_diagnose_task, "异常归因：分析最近的失败任务，给出原因与建议", "limit: 分析几轮，默认 5"),
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
        "get_account_pool": {"type": "object", "properties": {}},
        "get_selection_stats": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "列出账号数，默认 10"}}},
        "get_disabled_models": {"type": "object", "properties": {}},
        "get_health_check": {"type": "object", "properties": {}},
        "get_account_credentials": {"type": "object", "properties": {}},
        "get_task_ledger": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "列出任务数，默认 10"}}},
        "get_credit_breakdown": {"type": "object", "properties": {}},
        "get_api_keys": {"type": "object", "properties": {}},
        "get_notify_status": {"type": "object", "properties": {}},
        "get_credit_trend": {"type": "object", "properties": {}},
        "diagnose_task_failure": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "分析几轮失败任务，默认 5"}}},
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
# 阶段 5：写操作两段式确认（2026-10-14）
#
# 用户反馈（v0.9.71 的直接动因）：「我只是问他今天执行过任务吗？为何就直接
# 执行了」。结构性解法：**写操作永不被自然语言直接触发**，必须先经一次显式
# 确认，且确认逻辑是**纯确定性代码**，不经过 LLM 判断。
#
# 流程：
#   用户: 执行任务
#   助手: 【待确认】将启动一轮每日任务
#         · 影响：22 个账号（签到+成长任务+领奖）
#         · 预计：40-60 分钟
#         · 回复「确认」执行，「取消」放弃
#   用户: 确认
#   助手: 已启动（job_id: xxxx），完成后推送结果
#
# 设计要点：
#   1. **意图识别是确定性的**：`_WRITE_INTENTS` 关键词表 + 精确匹配，
#      与 v0.9.71 的 `_looks_like_question` 互补 —— 疑问句走查询，
#      陈述/命令句才可能进写意图；
#   2. **确认态与会话同生命周期**：复用 `_SESSIONS`，TTL 5 分钟（短于对话
#      的 30 分钟，因为确认是即时决策，放太久容易忘）；
#   3. **影响预告**：每个写操作带 `impact`（影响范围）与 `estimate`（耗时预估），
#      让用户在确认前就知道会发生什么；
#   4. **审计日志**：每次实际执行都记 `audit`（时间/会话/操作/参数），
#      便于事后追溯「谁在什么时候让助手做了什么」。
# ---------------------------------------------------------------------------

WRITE_CONFIRM_ENABLED = (os.getenv("AB_ASSISTANT_WRITE_CONFIRM", "1") or "1").strip() not in ("0", "false", "no")
WRITE_CONFIRM_TTL = float(os.getenv("AB_ASSISTANT_CONFIRM_TTL", "300") or 300)

_AUDIT: List[Dict[str, Any]] = []
_AUDIT_MAX = 200

# 写操作注册表：action -> (描述, 影响预告, 耗时预估, 执行器)
# 执行器签名：fn(args: dict) -> str（返回给用户的执行结果文本）
_WRITE_OPS: Dict[str, Dict[str, Any]] = {}


def _register_write_op(action: str, desc: str, impact: str, estimate: str):
    """注册一个写操作（装饰器用法）。"""
    def _wrap(fn):
        _WRITE_OPS[action] = {
            "desc": desc, "impact": impact, "estimate": estimate,
            "fn": fn, "keywords": [],
        }
        return fn
    return _wrap


def _write_intent(text: str) -> Optional[str]:
    """把用户输入解析成写操作 action；不是写意图则返回 None。

    确定性判据（按优先级）：
      1. 精确命令名（含同义词）；
      2. 含「执行/启动/跑 + 任务」等动词组合。
    疑问句**不在此处理** —— v0.9.71 已在 notify 层把疑问句截走，
    这里再兜一层：含疑问标记的一律不判为写意图（双保险）。
    """
    t = (text or "").strip().lower().strip("。！!，,~～.… ")
    if not t:
        return None
    # 双保险：疑问句永远不是写意图
    try:
        from gateway import notify as _nt
    except ImportError:
        try:
            import notify as _nt  # type: ignore
        except ImportError:
            _nt = None
    if _nt is not None and _nt._looks_like_question(text):
        return None
    for m in ("吗", "呢", "？", "?"):
        if m in t:
            return None

    # 精确命令（菜单按钮与手打命令）
    if t in ("执行任务", "启动任务", "跑任务", "运行任务", "run", "task_run"):
        return "run_daily"
    if t in ("签到", "全部签到", "checkin"):
        return "checkin_all"
    if t in ("中止任务", "停止任务", "取消任务", "abort", "abort_task"):
        return "abort_daily"
    if t in ("触发巡检", "立即巡检", "跑一次巡检", "probe_models"):
        return "probe_models"
    if t in ("开启通知", "打开通知", "notify_on"):
        return "notify_on"
    if t in ("关闭通知", "关掉通知", "notify_off"):
        return "notify_off"
    if t in ("重置选号统计", "清空选号统计", "reset_selections"):
        return "reset_selections"

    # 动词组合（自然语言下令，但不是疑问句）
    if ("任务" in t or "每日" in t) and any(v in t for v in ("执行", "启动", "跑", "运行", "来一轮", "跑一轮")):
        return "run_daily"
    if "签到" in t and any(v in t for v in ("执行", "去", "来", "全部")):
        return "checkin_all"
    if "巡检" in t and any(v in t for v in ("触发", "执行", "跑", "立即")):
        return "probe_models"
    return None


def _pending_write(session_id: str) -> Optional[Dict[str, Any]]:
    """取该会话待确认的写操作（过期则清除）。"""
    key = _session_key(session_id)
    now = time.time()
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(key) or {}
        p = s.get("pending_write")
        if not p:
            return None
        if float(p.get("expires") or 0) < now:
            s.pop("pending_write", None)
            return None
        return dict(p)


def _set_pending_write(session_id: str, action: str, args: Dict[str, Any]) -> None:
    key = _session_key(session_id)
    now = time.time()
    with _SESSIONS_LOCK:
        s = _SESSIONS.setdefault(key, {"history": [], "updated": now})
        s["pending_write"] = {
            "action": action, "args": dict(args or {}),
            "expires": now + WRITE_CONFIRM_TTL, "created": now,
        }
        s["updated"] = now


def _clear_pending_write(session_id: str) -> None:
    key = _session_key(session_id)
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(key)
        if s:
            s.pop("pending_write", None)


def _audit_write(session_id: str, action: str, args: Dict[str, Any], result: str) -> None:
    """审计日志：谁在什么时候让助手做了什么（内存环形缓冲，可观测）。"""
    _AUDIT.append({
        "ts": time.time(),
        "session": _session_key(session_id),
        "action": action,
        "args": {k: str(v)[:40] for k, v in (args or {}).items()},
        "ok": bool(result) and "失败" not in result[:20],
        "result": str(result)[:160],
    })
    del _AUDIT[:-_AUDIT_MAX]


def write_audit_log(limit: int = 20) -> List[Dict[str, Any]]:
    """最近若干条写操作审计（供排查/未来面板）。"""
    return list(_AUDIT[-max(1, int(limit)):])


_CONFIRM_WORDS = ("确认", "确定", "是的", "是", "好", "好的", "嗯", "可以",
                  "ok", "yes", "y", "执行", "确认执行", "去吧")
_CANCEL_WORDS = ("取消", "算了", "不用", "先不", "否", "不", "no", "n", "放弃")


def _confirm_intent(text: str) -> Optional[str]:
    """识别对【待确认】的回复：返回 "confirm" / "cancel" / None。"""
    t = (text or "").strip().lower().strip("。！!，,~～.… ")
    if not t:
        return None
    if t in _CANCEL_WORDS:
        return "cancel"
    if t in _CONFIRM_WORDS:
        return "confirm"
    # 复合句：「确认执行」「好的，执行吧」——含确认词且不含取消词
    if any(c in t for c in ("确认", "确定")) and not any(x in t for x in _CANCEL_WORDS):
        return "confirm"
    if t in ("执行", "去吧", "来吧"):
        return "confirm"
    return None


def _write_op_prompt(action: str) -> str:
    """待确认提示（含影响预告与耗时预估）。"""
    op = _WRITE_OPS.get(action) or {}
    return ("【待确认】%s\n"
            "  · 影响：%s\n"
            "  · 预计：%s\n"
            "  · 回复「确认」执行，「取消」放弃（%d 秒内有效）" % (
                op.get("desc") or action,
                op.get("impact") or "—",
                op.get("estimate") or "—",
                int(WRITE_CONFIRM_TTL)))


def try_write_flow(session_id: str, text: str) -> Optional[str]:
    """写操作两段式流程入口（在 LLM 之前拦截）。

    返回 None 表示「这不是写操作流程」，调用方继续走正常问答；
    返回字符串表示已由本流程处理（待确认提示 / 执行结果 / 取消回执）。

    调用方（notify.wecom_command_reply）在把文本交给助手前先过这里 ——
    这样写意图**完全不经过 LLM**，从结构上杜绝「问一句跑一轮」。
    """
    if not WRITE_CONFIRM_ENABLED:
        return None
    q = (text or "").strip()
    if not q:
        return None

    # 1) 已有待确认 → 先处理确认/取消
    pend = _pending_write(session_id)
    if pend:
        intent = _confirm_intent(q)
        if intent == "cancel":
            _clear_pending_write(session_id)
            return "已取消，未执行任何操作。"
        if intent == "confirm":
            action = str(pend.get("action") or "")
            args = pend.get("args") or {}
            _clear_pending_write(session_id)
            op = _WRITE_OPS.get(action)
            if not op:
                return "操作定义不存在：%s（已取消）" % action
            try:
                result = str(op["fn"](args))
            except Exception as e:
                result = "执行失败：%s: %s" % (type(e).__name__, str(e)[:100])
            _audit_write(session_id, action, args, result)
            return result
        # 既不是确认也不是取消 → 视为放弃本次确认，按新输入继续
        _clear_pending_write(session_id)

    # 2) 识别写意图 → 进入待确认
    action = _write_intent(q)
    if action and action in _WRITE_OPS:
        _set_pending_write(session_id, action, {})
        return _write_op_prompt(action)
    return None


# ---------------------------------------------------------------------------
# 阶段 5：注册 6 个写操作（只保留「影响可预期、可回滚或可重跑」的）
#
# 有意**排除**的危险操作（面板可做，助手不做）：
#   - 登录/改密/导入导出/删除全部 Key —— 涉及凭据与数据主权
#   - 账号级永久删除
# 这些留给设置页人工操作。
# ---------------------------------------------------------------------------

def _wb_post(path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """向 WebUI 代理发 POST（同容器 loopback）。"""
    return _http_json(WEBUI_BASE + path, timeout=30.0, method="POST",
                      body=json.dumps(body or {}).encode("utf-8")) or {}


@_register_write_op("run_daily", "将启动一轮每日任务（签到 + 成长任务 + 领奖）",
                    "全部参与账号（通常 20+ 个）", "约 40-60 分钟")
def _op_run_daily(_args: Dict[str, Any]) -> str:
    d = _wb_post("/api/wb-daily/run")
    if d.get("job_id"):
        return ("✅ 已启动一轮每日任务（job_id: %s）。\n"
                "完成后会自动推送结果；也可问我「任务跑完了吗」。" % d["job_id"])
    return "启动失败：%s" % (d.get("detail") or "可能已有任务在执行")


@_register_write_op("checkin_all", "将为全部账号执行签到",
                    "全部参与账号", "约 5-15 分钟")
def _op_checkin_all(_args: Dict[str, Any]) -> str:
    d = _wb_post("/api/checkin/all")
    return "✅ 签到已提交（%s）。" % (d.get("status") or "已开始")


@_register_write_op("abort_daily", "将中止当前正在执行的每日任务",
                    "当前运行中的那一轮（未完成的账号会中断）", "立即生效")
def _op_abort_daily(_args: Dict[str, Any]) -> str:
    # 先找正在跑的任务，避免对空跑 abort
    try:
        d = _http_json(WEBUI_BASE + "/api/wb-daily/jobs") or {}
        jobs = d.get("jobs") or []
    except Exception:
        jobs = []
    running = [j for j in jobs if j.get("status") == "running"]
    if not running:
        return "当前没有正在执行的任务，无需中止。"
    jid = running[0].get("job_id")
    _wb_post("/api/wb-daily/abort/%s" % jid)
    return "✅ 已请求中止任务 %s。" % jid


@_register_write_op("probe_models", "将立即执行一轮模型可用性巡检",
                    "按巡检配置覆盖的账号与模型", "约 5-20 分钟")
def _op_probe_models(_args: Dict[str, Any]) -> str:
    d = _wb_post("/api/model-health/run")
    if d.get("ok") is False:
        return "启动巡检失败：%s" % (d.get("error") or "未知原因")
    return "✅ 已启动一轮模型巡检，结果会自动写入禁用/启用状态。"


@_register_write_op("notify_on", "将开启通知推送总开关",
                    "所有已启用渠道（企微/Telegram）", "立即生效")
def _op_notify_on(_args: Dict[str, Any]) -> str:
    try:
        try:
            from gateway import notify as _nt
        except ImportError:
            import notify as _nt  # type: ignore
        _nt.update_config({"enabled": True})
        return "✅ 通知已开启。"
    except Exception as e:
        return "操作失败：%s" % str(e)[:80]


@_register_write_op("notify_off", "将关闭通知推送总开关",
                    "所有渠道的例行推送都会停止", "立即生效")
def _op_notify_off(_args: Dict[str, Any]) -> str:
    try:
        try:
            from gateway import notify as _nt
        except ImportError:
            import notify as _nt  # type: ignore
        _nt.update_config({"enabled": False})
        return "✅ 通知已关闭。"
    except Exception as e:
        return "操作失败：%s" % str(e)[:80]


# ---------------------------------------------------------------------------
# 模型选择
# ---------------------------------------------------------------------------

def _pick_model(rt: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """挑助手用的模型。

    优先级：设置页显式指定 > provider 的 /v1/models 里按 _FALLBACK_MODELS 挑
    > 目录第一个。拉不到目录时退回 _FALLBACK_MODELS[0]（网关不可用时至少
    让请求发出去，由上游报真实错误）。
    """
    rt = rt or _runtime()
    if not rt.get("enabled"):
        return None
    if rt.get("model"):
        return rt["model"]
    base = rt.get("base_url") or GATEWAY_BASE
    try:
        d = _http_json(base + "/v1/models", timeout=6.0,
                       api_key=rt.get("api_key") or "") or {}
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
    rt = _runtime()
    if not rt.get("enabled"):
        return ""
    model = _pick_model(rt)
    if not model:
        return ""
    base = rt.get("base_url") or GATEWAY_BASE
    api_key = rt.get("api_key") or ""
    sys_prompt = rt.get("systemPrompt") or SYSTEM_PROMPT

    messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
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
            base + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"})
        if api_key:
            req.add_header("Authorization", "Bearer %s" % api_key)
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
                    base + "/v1/chat/completions",
                    data=json.dumps(payload2).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"})
                if api_key:
                    req2.add_header("Authorization", "Bearer %s" % api_key)
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
                        base + "/v1/chat/completions",
                        data=json.dumps(payload2).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"})
                    if api_key:
                        req2.add_header("Authorization", "Bearer %s" % api_key)
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
