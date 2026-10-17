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

# 联网搜索（Tavily）：密钥沿用宿主已有的 TAVILY_API_KEY，不在本模块另存。
# 联网搜索（Tavily）：密钥优先读环境变量；环境变量缺失时回退读
# 持久化文件 /data/.autobuddy/.env.local（容器重建不丢）。
def _load_tavily_key() -> str:
    k = (os.getenv("TAVILY_API_KEY") or "").strip()
    if k:
        return k
    try:
        data_dir = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
        env_file = data_dir / ".env.local"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("TAVILY_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


TAVILY_API_KEY = _load_tavily_key()
TAVILY_ENDPOINT = "https://api.tavily.com/search"
# 会话历史裁剪的 token 上限（对齐 MoviePilot 的「最大上下文 Token 数量 (K)」）。
# 粗估：中文 1 字≈1 token，英文 4 字符≈1 token；取 max(len) 偏保守。
DEFAULT_MAX_CONTEXT_K = 256

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


# ---------------------------------------------------------------------------
# 联网搜索引擎注册表（2026-10-16 用户需求）
#
# 用户原话：「搜索引擎作为可选项，前端添加 UI 配置，用户自行配置搜索引擎，
# 也可以用其他的，而不是把我的 key 写死到仓库里，因为这是开源的」
#
# 因此密钥**一律不进仓库**：每个引擎的 api_key 存在用户自己的
# assistant_config.json 里（与 LLM provider 的 secret 语义完全一致：
# GET 不回明文、不提交=保留、空串=清除、有值=覆盖）。
#
# 设计对齐 PROVIDERS：
#   - default_base_url + base_url_presets：预设地址，省得手打
#   - supports_api_key：是否需要密钥
#   - extra_fields：个别引擎要的附加参数（如 Google CSE 的 cx）
#   - desc / docs：设置页上的说明与申请地址
#
# 每个引擎的请求/响应格式不同，由 _SEARCH_ADAPTERS 里的适配函数归一化成
# 统一的 (answer, [{title, url, content}])，工具层不关心差异。
# ---------------------------------------------------------------------------
SEARCH_PROVIDERS: List[Dict[str, Any]] = [
    {
        "key": "tavily",
        "name": "Tavily",
        "desc": "专为 AI 设计的搜索 API，返回摘要+结果，开箱即用。默认选项。",
        "default_base_url": "https://api.tavily.com",
        "base_url_presets": [
            {"id": "tavily", "label": "Tavily 官方", "value": "https://api.tavily.com"},
        ],
        "base_url_editable": True,
        "supports_api_key": True,
        "api_key_label": "API Key",
        "api_key_hint": "留空且已配置过 = 保持不变；填值 = 覆盖；明确提交空串 = 清除",
        "docs": "https://tavily.com（免费额度 1000 次/月）",
    },
    {
        "key": "serper",
        "name": "Serper（Google 结果）",
        "desc": "返回 Google 真实搜索结果，含知识图谱与相关问题；延迟低。",
        "default_base_url": "https://google.serper.dev",
        "base_url_presets": [
            {"id": "serper", "label": "Serper 官方", "value": "https://google.serper.dev"},
        ],
        "base_url_editable": True,
        "supports_api_key": True,
        "api_key_label": "API Key",
        "api_key_hint": "请求头 X-API-KEY 使用的密钥",
        "docs": "https://serper.dev（免费 2500 次）",
    },
    {
        "key": "bing",
        "name": "Bing Web Search",
        "desc": "微软必应网页搜索，Azure 门户申请，结果稳定。",
        "default_base_url": "https://api.bing.microsoft.com/v7.0",
        "base_url_presets": [
            {"id": "bing", "label": "Bing v7 官方", "value": "https://api.bing.microsoft.com/v7.0"},
        ],
        "base_url_editable": True,
        "supports_api_key": True,
        "api_key_label": "Subscription Key",
        "api_key_hint": "请求头 Ocp-Apim-Subscription-Key 使用的密钥",
        "docs": "https://www.microsoft.com/bing/apis/bing-web-search-api",
    },
    {
        "key": "google_cse",
        "name": "Google Custom Search",
        "desc": "Google 官方自定义搜索，需要 API Key 与搜索引擎 ID（cx）两项。",
        "default_base_url": "https://www.googleapis.com/customsearch/v1",
        "base_url_presets": [
            {"id": "google_cse", "label": "Google CSE 官方", "value": "https://www.googleapis.com/customsearch/v1"},
        ],
        "base_url_editable": True,
        "supports_api_key": True,
        "api_key_label": "API Key",
        "api_key_hint": "Google Cloud 控制台申请的 API Key",
        "extra_fields": [
            {"key": "cx", "label": "搜索引擎 ID（cx）", "type": "text",
             "desc": "在 cse.google.com 创建搜索引擎后获得，形如 0123456789abcdef:hijk。"},
        ],
        "docs": "https://developers.google.com/custom-search/v1/overview",
    },
    {
        "key": "bocha",
        "name": "博查搜索",
        "desc": "国内可直连的搜索 API，中文结果好，无需特殊网络环境。",
        "default_base_url": "https://api.bochaai.com/v1",
        "base_url_presets": [
            {"id": "bocha", "label": "博查官方", "value": "https://api.bochaai.com/v1"},
        ],
        "base_url_editable": True,
        "supports_api_key": True,
        "api_key_label": "API Key",
        "api_key_hint": "请求头 Authorization: Bearer <key> 使用的密钥",
        "docs": "https://open.bochaai.com",
    },
    {
        "key": "searxng",
        "name": "SearXNG（自建）",
        "desc": "自托管元搜索，聚合 Google/Bing/DDG 等来源，无需申请密钥。",
        "default_base_url": "http://127.0.0.1:8080",
        "base_url_presets": [
            {"id": "local", "label": "本机实例", "value": "http://127.0.0.1:8080"},
        ],
        "base_url_editable": True,
        "supports_api_key": False,
        "extra_fields": [],
        "docs": "https://docs.searxng.org（需在 settings.yml 开启 JSON 格式）",
    },
]

_SEARCH_BY_KEY = {p["key"]: p for p in SEARCH_PROVIDERS}


def _blank_config() -> Dict[str, Any]:
    """配置默认值。

    字段分层（2026-10-15 照 MoviePilot 智能助手设置页逐项对照后补齐）：
      - 已有（用户明确：LLM 那块已做好，不动）：enabled / provider / model /
        systemPrompt / providers
      - 新补 6 项：maxContextK / temperature / reasoningEffort /
        outputLanguage / userAgent / verbose
      - 联网搜索（2026-10-16 重构）：webSearch 语义改为
        "off" | <searchProvider 的 key>；searchProvider 选引擎，
        searchProviders 存每个引擎的 base_url / api_key / extra。
        密钥**一律不进仓库**（开源项目），只落在用户自己的配置文件中。
    """
    return {
        "enabled": True,
        "provider": _default_provider(),
        "model": "",                      # 空 = 自动挑选
        "systemPrompt": "",               # 空 = 用内置 SYSTEM_PROMPT
        # —— 以下为照 MP 补齐的通用项 ——
        "maxContextK": DEFAULT_MAX_CONTEXT_K,   # 会话历史 token 上限（千）
        "temperature": "",                # 空 = 模型默认；"0" = 显式 0
        "reasoningEffort": "auto",        # off|auto|minimal|low|medium|high|max|xhigh
        "outputLanguage": "zh-CN",        # 强制输出语言
        "userAgent": "",                  # 空 = SDK 默认
        "verbose": False,                 # 啰嗦模式（MP 同名）
        # —— 联网搜索（引擎可自选，密钥自备）——
        "webSearch": "off",               # off | tavily | serper | bing | ...
        "searchProvider": "tavily",       # 当前选用的搜索引擎
        "searchProviders": {
            p["key"]: {
                "base_url": p.get("default_base_url") or "",
                "api_key": "",
                "extra": {f["key"]: "" for f in (p.get("extra_fields") or [])},
            }
            for p in SEARCH_PROVIDERS
        },
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
                    # 嵌套结构要深合并：直接 update 会把整块替换掉，
                    # 导致新增引擎字段（extra）在旧配置文件里缺失。
                    for nest in ("providers", "searchProviders"):
                        provs = raw.get(nest)
                        if not isinstance(provs, dict):
                            continue
                        for k, v in provs.items():
                            if k not in cfg[nest] or not isinstance(v, dict):
                                continue
                            slot = cfg[nest][k]
                            slot.update({kk: vv for kk, vv in v.items() if kk in slot})
                            # extra 是三层嵌套，同样要逐键合并
                            extra = v.get("extra")
                            if isinstance(extra, dict):
                                base = slot.setdefault("extra", {})
                                if isinstance(base, dict):
                                    base.update(extra)
        except Exception:
            pass
        return cfg


def _save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并保存；secret 语义：不提交=保留、空串=清除、有值=覆盖。"""
    with _CONFIG_LOCK:
        cfg = _load_config()
        body = patch or {}
        for k in ("enabled", "provider", "model", "systemPrompt",
                  "maxContextK", "temperature", "reasoningEffort",
                  "outputLanguage", "userAgent", "verbose", "webSearch",
                  "searchProvider"):
            if k in body:
                cfg[k] = body[k]
        # LLM provider（原有）
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
        # 搜索引擎 provider（2026-10-16 新增，secret 语义与上面完全一致）
        sprovs = body.get("searchProviders")
        if isinstance(sprovs, dict):
            for pk, pv in sprovs.items():
                if pk not in cfg["searchProviders"] or not isinstance(pv, dict):
                    continue
                slot = cfg["searchProviders"][pk]
                for k in ("base_url", "api_key"):
                    if k not in pv:
                        continue          # 未提交 → 保留
                    v = pv[k]
                    if v is None:
                        continue          # 保留
                    slot[k] = str(v or "").strip()
                extra = pv.get("extra")
                if isinstance(extra, dict):
                    base = slot.setdefault("extra", {})
                    if isinstance(base, dict):
                        for ek, ev in extra.items():
                            if ek in base and ev is not None:
                                base[ek] = str(ev or "").strip()
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        except Exception:
            pass
        return cfg


def _runtime() -> Dict[str, Any]:
    """当前生效的运行时配置（provider + base_url + key + model + 通用项）。"""
    cfg = _load_config()
    if not cfg.get("enabled", True):
        return {"enabled": False}
    pk = str(cfg.get("provider") or _default_provider())
    prov = _PROVIDER_BY_KEY.get(pk) or _PROVIDER_BY_KEY[_default_provider()]
    slot = (cfg.get("providers") or {}).get(prov["key"]) or {}
    base = str(slot.get("base_url") or "").strip() or prov.get("default_base_url") or ""
    # 温度：空串 = 不传（用模型默认）；"0" 是有效值，必须保留。
    temp_raw = cfg.get("temperature", "")
    temperature: Optional[float] = None
    if isinstance(temp_raw, (int, float)):
        temperature = float(temp_raw)
    elif str(temp_raw).strip() != "":
        try:
            temperature = float(str(temp_raw).strip())
        except ValueError:
            temperature = None
    return {
        "enabled": True,
        "provider": prov["key"],
        "base_url": base.rstrip("/"),
        "api_key": str(slot.get("api_key") or "").strip(),
        "model": str(cfg.get("model") or "").strip(),
        "systemPrompt": str(cfg.get("systemPrompt") or "").strip(),
        "maxContextK": int(cfg.get("maxContextK") or DEFAULT_MAX_CONTEXT_K),
        "temperature": temperature,
        "reasoningEffort": str(cfg.get("reasoningEffort") or "auto").strip(),
        "outputLanguage": str(cfg.get("outputLanguage") or "zh-CN").strip(),
        "userAgent": str(cfg.get("userAgent") or "").strip(),
        "verbose": bool(cfg.get("verbose")),
        "webSearch": str(cfg.get("webSearch") or "off").strip(),
    }


# 设置页注册表：照 MoviePilot 智能助手设置项逐项对齐（仅列 AutoBuddy 适用的）。
SETTING_FIELDS: List[Dict[str, Any]] = [
    {"key": "enabled", "label": "启用智能助手", "type": "bool",
     "desc": "关闭后助手不再响应，命令路径不受影响（等同 AB_ASSISTANT_ENABLED=0）。"},
    {"key": "verbose", "label": "啰嗦模式", "type": "bool",
     "desc": "开启后回复更详细（带过程与结论）；关闭则只给结论，更简短。"},
    {"key": "maxContextK", "label": "LLM 最大上下文 Token 数量 (K)", "type": "number",
     "desc": "会话历史的最大 Token 上限（千），超出后自动裁剪旧消息，节省消耗并防超限。",
     "min": 8, "max": 1024},
    {"key": "temperature", "label": "温度参数", "type": "number",
     "desc": "控制回复随机性；留空使用模型默认值，填 0 表示显式使用温度 0，通常建议 0-2。",
     "min": 0, "max": 2},
    {"key": "reasoningEffort", "label": "思考模式 / 深度", "type": "select",
     "desc": "思考深度：off/auto/minimal/low/medium/high/max/xhigh；不支持的级别会按模型能力自动映射。",
     "options": ["off", "auto", "minimal", "low", "medium", "high", "max", "xhigh"]},
    {"key": "outputLanguage", "label": "助手输出语言", "type": "text",
     "desc": "强制助手使用所选语言回复，默认 zh-CN。"},
    {"key": "userAgent", "label": "User-Agent", "type": "text",
     "desc": "OpenAI 兼容接口请求使用的 User-Agent，留空则使用默认值。"},
    # 联网搜索：2026-10-16 起引擎可自选（密钥由用户在下方自行配置，
    # 不再内置任何他人 key）。选项由 SEARCH_PROVIDERS 动态生成 ——
    # 以后加引擎只需改注册表，这里不用动。
    {"key": "webSearch", "label": "联网搜索", "type": "select",
     "desc": "开启后助手可实时联网检索；关闭则只用内置只读工具。"
             "选好引擎后请在下方填写该引擎的地址与 API Key。",
     "options": ["off"] + [p["key"] for p in SEARCH_PROVIDERS]},
]


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
    # 搜索引擎（2026-10-16）：注册表 + 每个引擎的 base_url / 密钥就绪状态 /
    # 附加字段当前值。密钥同样只回 has_value。
    search_out: Dict[str, Any] = {}
    for p in SEARCH_PROVIDERS:
        slot = (cfg.get("searchProviders") or {}).get(p["key"]) or {}
        extra_out: Dict[str, Any] = {}
        slot_extra = slot.get("extra") if isinstance(slot.get("extra"), dict) else {}
        for f in (p.get("extra_fields") or []):
            extra_out[f["key"]] = str(slot_extra.get(f["key"]) or "")
        search_out[p["key"]] = {
            "base_url": str(slot.get("base_url") or p.get("default_base_url") or ""),
            "api_key": {"has_value": bool(str(slot.get("api_key") or "").strip())},
            "extra": extra_out,
        }
    return {
        "registry": PROVIDERS,
        "settings": SETTING_FIELDS,
        "searchRegistry": SEARCH_PROVIDERS,
        # 兼容字段：老前端只认 tavily.has_value，保留但语义改为
        # 「当前选中的搜索引擎密钥是否就绪」。
        "tavily": {"has_value": bool(TAVILY_API_KEY)},
        "config": {
            "enabled": bool(cfg.get("enabled", True)),
            "provider": str(cfg.get("provider") or _default_provider()),
            "model": str(cfg.get("model") or ""),
            "systemPrompt": str(cfg.get("systemPrompt") or ""),
            "maxContextK": int(cfg.get("maxContextK") or DEFAULT_MAX_CONTEXT_K),
            "temperature": cfg.get("temperature", ""),
            "reasoningEffort": str(cfg.get("reasoningEffort") or "auto"),
            "outputLanguage": str(cfg.get("outputLanguage") or "zh-CN"),
            "userAgent": str(cfg.get("userAgent") or ""),
            "verbose": bool(cfg.get("verbose")),
            "webSearch": str(cfg.get("webSearch") or "off"),
            "searchProvider": str(cfg.get("searchProvider") or "tavily"),
            "providers": provs_out,
            "searchProviders": search_out,
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

## 数字对不上时，必须双查

用户报出一个数字（如「签到不是 500 吗」）而你的数据源是另一个值时，
**不要二选一就下结论**。按这个顺序：

1. **本地查实时字段** —— 优先用对应工具拿活数据
   （签到类问题用 `get_checkin_activity`，它有 `daily_credit` /
   `week_progress` / `streak_bonus_credit` 这些字段；
   不要拿规则表的常量值冒充实时值）。
2. **联网查官方规则** —— 本地字段解释不了时调 `web_search`
   查官方说明、社区实测，看是不是**活动变更**或**往期限时活动**。
3. **两个都查完再答**，明确说清：
   - 本地现在实际是多少；
   - 用户说的那个数可能对应什么（月赠 / 一次性任务 / 往期活动 /
     累计值被误当成单日值 —— 例如「连签 5 天累计 500」≠「每天 500」）；
   - 结论以哪边为准、为什么。

宁可多说两句「这可能是累计值/月赠/一次性任务」，也不要不查就否定用户。

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
    """取会话历史，并按 maxContextK 裁剪（照 MP 的上下文 Token 上限语义）。

    裁剪策略：从最新往旧累加，超上限就丢弃更早的整轮，保证最新的
    对话一定在上下文里。粗估 1 字≈1 token（中文）偏保守，宁可少留几轮。
    """
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(_session_key(session_id)) or {}
        hist = list(s.get("history") or [])
    try:
        limit_k = int(_load_config().get("maxContextK") or DEFAULT_MAX_CONTEXT_K)
    except Exception:
        limit_k = DEFAULT_MAX_CONTEXT_K
    budget = max(1, limit_k) * 1000
    if not hist:
        return hist
    kept: List[Dict[str, str]] = []
    used = 0
    for msg in reversed(hist):
        cost = len(str(msg.get("content") or ""))
        if kept and used + cost > budget:
            break
        kept.append(msg)
        used += cost
    kept.reverse()
    return kept


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
    """请求指标：从 summary 子对象取，并说明统计窗口语义。

    2026-10-15 修 bug：原实现把顶层当指标容器（`for k in (...): if k in d`），
    但真实结构是 `{"summary": {...}, "recent": [...]}` —— 键都在 summary 里，
    于是恒返回一个光杆的「请求指标：」。
    """
    d = _http_json(WEBUI_BASE + "/api/reqstats?limit=50") or {}
    if not d:
        return "暂无请求指标"
    s = d.get("summary") or {}
    if not s:
        return "暂无请求指标"
    win = s.get("windowCount")
    parts = ["请求指标%s：" % ("（窗口内 %s 次）" % win if win is not None else "")]
    labels = (("total", "总请求"), ("success", "成功"),
              ("httpError", "HTTP 错"), ("streamError", "流中断"),
              ("successRate", "成功率"), ("avgMs", "平均耗时 ms"),
              ("p50Ms", "P50 ms"), ("p95Ms", "P95 ms"))
    for k, label in labels:
        if s.get(k) is not None:
            v = s.get(k)
            try:
                v = round(float(v), 1) if k not in ("total", "success", "httpError", "streamError", "windowCount") else v
            except (TypeError, ValueError):
                pass
            parts.append("  · %s：%s" % (label, v))
    recent = d.get("recent") or []
    if recent:
        parts.append("  · 最近请求：可问我「最近请求明细」")
    elif win == 0:
        parts.append("  ·（窗口内无请求，指标为累计口径；刚重启时属正常）")
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


# ---------------------------------------------------------------------------
# 补全工具（2026-10-15，用户要求「补全所有接口，使助手真正可用可交付」）
#
# 盘点依据：对外端点 64 个，助手原覆盖 14 个。下面按功能域补齐**只读**部分；
# 认证类 / 删除类 / import / export / 助手自身配置 属红线，永久不给工具。
# ---------------------------------------------------------------------------
def _fmt_ts(ts: Any, fmt: str = "%m-%d %H:%M") -> str:
    """时间戳 → 可读时间（本地时区）。"""
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime(fmt)
    except Exception:
        return "?"


def _tool_task_runs(args: Dict[str, Any]) -> str:
    """轮次历史：每轮任务的开始/结束/状态/账号数。"""
    try:
        limit = int((args or {}).get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(50, limit))
    d = _http_json(WEBUI_BASE + "/api/wb-daily/runs?limit=%d" % limit) or {}
    runs = d if isinstance(d, list) else (d.get("runs") or [])
    if not runs:
        return "暂无任务轮次记录。"
    st_filter = str((args or {}).get("status") or "").strip().lower()
    if st_filter:
        runs = [r for r in runs if str(r.get("status") or "").lower() == st_filter]
        if not runs:
            return "没有状态为 %s 的轮次。" % st_filter
    lines = ["任务轮次（%d 条）：" % len(runs)]
    for r in runs:
        st, fin = r.get("started_at"), r.get("finished_at")
        dur = ""
        try:
            if st and fin:
                dur = "（%.0f 分钟）" % ((float(fin) - float(st)) / 60.0)
        except Exception:
            dur = ""
        lines.append("  · %s %s · %s%s · 账号 %s · %s" % (
            _fmt_ts(st), str(r.get("status")), str(r.get("mode") or "?"),
            dur, r.get("accounts") or "?", str(r.get("job_id"))[:8]))
    return "\n".join(lines)


def _tool_task_detail(args: Dict[str, Any]) -> str:
    """单轮详情：失败原因 / 输出摘要。"""
    jid = str((args or {}).get("job_id") or "").strip()
    if not jid:
        return "请提供 job_id（可先用 get_task_runs 查看）。"
    # runs 里拿兜底信息（status 接口只对内存态有效，重启后 404）
    d = _http_json(WEBUI_BASE + "/api/wb-daily/runs?limit=50") or {}
    runs = d if isinstance(d, list) else (d.get("runs") or [])
    hit = None
    for r in runs:
        if str(r.get("job_id")) == jid or str(r.get("job_id")).startswith(jid):
            hit = r
            break
    if not hit:
        return "找不到轮次 %s（可用 get_task_runs 查看现有轮次）。" % jid
    lines = ["轮次 %s：" % hit.get("job_id"),
             "  · 状态：%s（%s）" % (hit.get("status"), hit.get("mode")),
             "  · 开始：%s" % _fmt_ts(hit.get("started_at"), "%Y-%m-%d %H:%M"),
             "  · 结束：%s" % _fmt_ts(hit.get("finished_at"), "%Y-%m-%d %H:%M"),
             "  · 账号数：%s" % (hit.get("accounts") or 0)]
    summ = hit.get("summary") or {}
    if isinstance(summ, dict):
        if summ.get("detail"):
            lines.append("  · 说明：%s" % str(summ["detail"])[:300])
        if summ.get("rc") is not None:
            lines.append("  · 返回码：%s" % summ.get("rc"))
        for s in (summ.get("summary") or [])[:6]:
            lines.append("    - %s" % str(s)[:160])
    return "\n".join(lines)


def _tool_task_accounts(args: Dict[str, Any]) -> str:
    """某轮涉及哪些账号：等级/连续天数/积分/完成进度/剩余任务。"""
    jid = str((args or {}).get("job_id") or "").strip()
    if not jid:
        return "请提供 job_id（可先用 get_task_runs 查看）。"
    d = _http_json(WEBUI_BASE + "/api/wb-daily/run-accounts/%s" % jid) or {}
    accts = d.get("accounts") or []
    if not accts:
        return "轮次 %s 没有账号明细记录。" % jid
    lines = ["轮次 %s 的账号（%d 个）：" % (jid[:8], len(accts))]
    for a in accts[:12]:
        rest = a.get("rest") or []
        lines.append("  · %s Lv%s 连续%s天 %s/%s" % (
            a.get("account"), a.get("level") or "?", a.get("streak") or "?",
            a.get("done") or 0, a.get("total") or 0))
        if rest:
            lines.append("      未完成：%s" % "、".join(str(x) for x in rest[:5]))
    if len(accts) > 12:
        lines.append("  ·（其余 %d 个略）" % (len(accts) - 12))
    return "\n".join(lines)


def _tool_task_catalog(_args: Dict[str, Any]) -> str:
    """任务目录：有哪些任务、哪些能自动跑、哪些要手动。"""
    d = _http_json(WEBUI_BASE + "/api/wb-daily/catalog") or {}
    groups = d.get("groups") or []
    if not groups:
        return "暂无任务目录。"
    lines = ["任务目录："]
    manual = []
    for g in groups:
        items = g.get("items") or []
        lines.append("  【%s】%d 项" % (g.get("group"), len(items)))
        for it in items[:8]:
            tag = "自动" if it.get("auto") else "**需手动**"
            lines.append("    - %s（%s）" % (it.get("name"), tag))
            if not it.get("auto") and it.get("note"):
                manual.append("%s：%s" % (it.get("name"), str(it["note"])[:120]))
        if len(items) > 8:
            lines.append("    ·（其余 %d 项略）" % (len(items) - 8))
    if manual:
        lines.append("  ⚠️ 需手动处理的任务：")
        for m in manual:
            lines.append("    - %s" % m)
    return "\n".join(lines)


def _tool_task_config(_args: Dict[str, Any]) -> str:
    """任务配置：开关/间隔/运行模式/签到开关/保留天数。"""
    d = _http_json(WEBUI_BASE + "/api/wb-daily/config") or {}
    c = d.get("config") or {}
    if not c:
        return "暂无任务配置。"
    return ("任务配置：\n"
            "  · 启用：%s\n"
            "  · 间隔：%s 小时\n"
            "  · 运行模式：%s\n"
            "  · 签到开关：%s\n"
            "  · 记录保留：%s 天\n"
            "  · 懒刷新：%s 小时\n"
            "  · 上次执行：%s" % (
                "开" if c.get("enabled") else "关",
                c.get("interval_hours"), c.get("run_mode"),
                "开" if c.get("checkin_enabled") else "关",
                c.get("keepalive_days"), c.get("lazy_refresh_hours"),
                _fmt_ts(c.get("last_run_at"), "%Y-%m-%d %H:%M")))


def _tool_task_health(_args: Dict[str, Any]) -> str:
    """任务服务健康：是否运行中、下次执行时间。"""
    d = _http_json(WEBUI_BASE + "/api/wb-daily/health") or {}
    if not d:
        return "任务服务无响应。"
    return ("任务服务：\n"
            "  · 状态：%s\n"
            "  · 正在执行：%s\n"
            "  · 上次执行：%s\n"
            "  · 下次计划：%s" % (
                "正常" if d.get("ok") else "异常",
                ("是（%s）" % str(d.get("running_job_id"))[:8]) if d.get("running") else "否",
                _fmt_ts(d.get("last_run_at"), "%Y-%m-%d %H:%M"),
                _fmt_ts(d.get("next_due_at"), "%Y-%m-%d %H:%M")))


def _tool_checkin_activity(_args: Dict[str, Any]) -> str:
    """签到活动详情：本期每日积分、连签/累计、周进度、连签奖励、活动周期。

    2026-10-17 用户反馈「助手答不出签到 500 积分」而新增。
    根因：此前 30 个工具没有一个读 /api/checkin/status，
    问「签到给多少分」只能拿积分规则表（100/天）硬答，
    看不到 daily_credit / week_progress / streak_bonus 这些活数据。

    数据源：/api/checkin/status（stale-while-revalidate 缓存，最多分钟级陈旧）。
    取不到时说「查不到」，不要用规则表的 100 兜底冒充实时值。
    """
    d = _http_json(WEBUI_BASE + "/api/checkin/status", timeout=25.0) or {}
    accs = [a for a in (d.get("accounts") or []) if a]
    if not accs:
        return "签到状态暂时查不到（接口无返回）。"

    # 按活动聚合：同一期活动的字段在账号间应当一致，取众数值而非首个 ——
    # 个别账号字段缺失（None）时不至于把整行结论带偏。
    from collections import Counter

    def majority(key):
        vals = [str((a.get("raw") or {}).get(key)) for a in accs
                if (a.get("raw") or {}).get(key) is not None]
        if not vals:
            return None
        return Counter(vals).most_common(1)[0][0]

    def _num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    name = majority("activity_name") or "签到活动"
    theme = majority("theme_name")
    season = _num(majority("season"))
    daily = _num(majority("daily_credit"))
    streak = _num(majority("streak_days"))
    total = _num(majority("total_credits"))
    week_n = _num(majority("week_checkin_days"))
    bonus_c = _num(majority("streak_bonus_credit"))
    bonus_d = _num(majority("streak_bonus_days"))
    is_streak_day = majority("is_streak_day")
    next_streak = _num(majority("next_streak_day"))
    start = majority("start_time")
    end = majority("end_time")
    claim_text = majority("claim_button_text")
    checked = sum(1 for a in accs if a.get("todayCheckedIn"))

    # 周进度：多数账号一致，直接取一个有值的账号
    week_prog = None
    for a in accs:
        wp = (a.get("raw") or {}).get("week_progress")
        if isinstance(wp, list) and wp:
            week_prog = wp
            break

    lines = ["签到活动（%s%s）：" % (
        name,
        (" · %s" % theme) if theme and theme != name else "")]
    if season:
        lines.append("  · 期数：第 %s 期" % season)
    lines.append("  · 每日积分：%s" % ("%d 积分/天" % daily if daily is not None else "未知"))
    if streak is not None or total is not None:
        lines.append("  · 连签 %s 天，累计 %s 积分"
                     % (streak if streak is not None else "?",
                        "{:,}".format(total) if total is not None else "?"))
    if week_n is not None or week_prog:
        if week_prog:
            marks = "".join("✓" if x else "·" for x in week_prog)
            lines.append("  · 本周进度：%s 天/7（%s）" % (week_n if week_n is not None else sum(1 for x in week_prog if x), marks))
        else:
            lines.append("  · 本周进度：%s 天/7" % week_n)
    if bonus_c or bonus_d:
        lines.append("  · 连签奖励：满 %s 天送 %s 积分" % (bonus_d, bonus_c))
    if is_streak_day == "True":
        lines.append("  · 今天就是连签奖励日")
    elif next_streak:
        lines.append("  · 距下一连签奖励 %s 天" % next_streak)
    lines.append("  · 今日已签到：%d/%d 个账号" % (checked, len(accs)))
    if claim_text:
        lines.append("  · 客户端按钮：%s" % claim_text)
    if start or end:
        lines.append("  · 活动周期：%s ~ %s" % (start or "?", end or "?"))
    lines.append("")
    lines.append("注：以上为接口实时字段；若与客户端页面不一致，"
                 "以页面为准并告诉我，我修正口径。")
    return "\n".join(lines)


def _tool_task_items(args: Dict[str, Any]) -> str:
    """任务执行明细：每个子任务的最近结果与说明。"""
    try:
        limit = int((args or {}).get("limit") or 12)
    except (TypeError, ValueError):
        limit = 12
    limit = max(1, min(40, limit))
    d = _http_json(WEBUI_BASE + "/api/wb-daily/tasks") or {}
    tasks = d.get("tasks") or []
    if not tasks:
        return "暂无任务执行明细。"
    lines = ["任务执行明细（最近 %d 条，共 %d）：" % (min(limit, len(tasks)), len(tasks))]
    for t in tasks[:limit]:
        lines.append("  · %s %s [%s] %s" % (
            _fmt_ts(t.get("occurred_at")), t.get("task_name"),
            t.get("result") or "?", str(t.get("account") or "")[:16]))
        det = str(t.get("detail") or "").strip()
        if det:
            lines.append("      %s" % det[:150])
    return "\n".join(lines)


def _tool_account_health(_args: Dict[str, Any]) -> str:
    """账号一致性自检（凭据侧 × 模型侧并排）。慢接口，超时给足。"""
    try:
        d = _http_json(WEBUI_BASE + "/api/account-health/audit", timeout=120.0) or {}
    except Exception as e:
        return ("账号自检未完成（%s）：该接口要对每个账号跑一次凭据探测"
                "加数次模型探测，账号多时耗时较长，可直接在面板上重试。"
                % str(e)[:80])
    rows = d.get("rows") or d.get("accounts") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not rows:
        return "账号自检返回为空（可能在运行中或未配置）。"
    lines = ["账号自检（%d 个）：" % len(rows)]
    for r in rows[:15]:
        if not isinstance(r, dict):
            continue
        lines.append("  · %s：凭据=%s 模型=%s" % (
            r.get("name") or r.get("account") or "?",
            r.get("credential") or r.get("credentialStatus") or "?",
            r.get("model") or r.get("modelStatus") or "?"))
        note = r.get("note") or r.get("detail")
        if note:
            lines.append("      %s" % str(note)[:130])
    return "\n".join(lines)


def _tool_account_models(args: Dict[str, Any]) -> str:
    """账号 × 模型覆盖：每个账号能用哪些模型、共禁用多少。"""
    d = _http_json(WEBUI_BASE + "/api/account-models", timeout=30.0) or {}
    accts = d.get("accounts") or {}
    if isinstance(accts, list):
        accts = {str(i): a for i, a in enumerate(accts)}
    if not accts:
        return "暂无账号模型配置。"
    cat = d.get("catalog") or []
    lines = ["账号模型覆盖（%d 个账号，目录共 %d 个模型，全局禁用 %s）：" % (
        len(accts), len(cat) if hasattr(cat, "__len__") else 0,
        d.get("disabledTotal"))]
    name = str((args or {}).get("account") or "").strip()
    rows = []
    for v in accts.values():
        if not isinstance(v, dict):
            continue
        if name and name not in str(v.get("name")) and name.lower() not in [str(x).lower() for x in (v.get("aliases") or [])]:
            continue
        rows.append(v)
    if not rows:
        rows = list(accts.values())[:12]
    for v in rows[:12]:
        if not isinstance(v, dict):
            continue
        ms = v.get("models") or []
        lines.append("  · %s：%d 个模型%s" % (
            v.get("name"), len(ms),
            ("（" + "、".join(str(x) for x in ms[:6]) + "…）") if ms else ""))
    if len(rows) > 12:
        lines.append("  ·（其余 %d 个略）" % (len(rows) - 12))
    return "\n".join(lines)


def _tool_token_stats(args: Dict[str, Any]) -> str:
    """Token 用量：按天统计（输入/输出/缓存命中率）。"""
    try:
        days = int((args or {}).get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(30, days))
    d = _http_json(WEBUI_BASE + "/api/token-stats", timeout=30.0) or {}
    src = d.get("sources") or []
    if not src:
        return "暂无 Token 用量数据。"
    lines = ["Token 用量（最近 %d 天）：" % days]
    for s in src[:2]:
        # requests / sessions 是**列表**（元素为 dict），不是计数 —— 需取长度
        reqs = s.get("requests")
        sess = s.get("sessions")
        n_req = len(reqs) if isinstance(reqs, (list, dict)) else (reqs or 0)
        n_ses = len(sess) if isinstance(sess, (list, dict)) else (sess or 0)
        lines.append("  【%s】%s 次请求 / %s 个会话" % (
            s.get("source") or "?", n_req, n_ses))
        daily = (s.get("daily") or [])[-days:]
        for dy in daily:
            hit = dy.get("cacheHitRate")
            hit_s = ("%.0f%%" % (float(hit) * 100)) if isinstance(hit, (int, float)) else "?"
            lines.append("    - %s：%s tokens（入 %s / 出 %s，缓存命中 %s）" % (
                dy.get("key"), _fmt_num(dy.get("total")),
                _fmt_num(dy.get("input")), _fmt_num(dy.get("output")), hit_s))
    return "\n".join(lines)


def _tool_reqlog(args: Dict[str, Any]) -> str:
    """请求日志明细：最近请求的模型/账号/耗时/结果。"""
    try:
        limit = int((args or {}).get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(30, limit))
    d = _http_json(WEBUI_BASE + "/api/reqstats?limit=%d" % limit, timeout=20.0) or {}
    recent = d.get("recent") or []
    if not recent:
        # 窗口内无请求是正常态（比如刚重启/空闲）—— 如实说明，不当作错误
        summ = d.get("summary") or {}
        return ("统计窗口内暂无请求明细（窗口内请求数：%s）。"
                "可稍后再问，或直接看累计指标（问「请求成功率」）。"
                % summ.get("windowCount", 0))
    lines = ["最近请求（%d 条）：" % len(recent)]
    for r in recent[:limit]:
        lines.append("  · %s %s @%s %sms [%s]" % (
            _fmt_ts(r.get("ts"), "%H:%M:%S"), r.get("model") or "?",
            r.get("account") or "?",
            ("%.0f" % float(r.get("total_ms"))) if r.get("total_ms") else "?",
            r.get("outcome") or "?"))
    return "\n".join(lines)


def _tool_notify_config(_args: Dict[str, Any]) -> str:
    """通知配置详情：各渠道就绪状态（密钥只显示是否已配）。"""
    d = _http_json(WEBUI_BASE + "/api/notify", timeout=20.0) or {}
    chans = d.get("channels") or {}
    if not chans:
        return "暂无通知渠道配置。"
    lines = ["通知配置（总开关：%s）：" % ("开" if d.get("enabled") else "关")]
    for k, v in chans.items():
        if not isinstance(v, dict):
            continue
        lines.append("  【%s】就绪：%s / 启用：%s" % (
            k, "是" if v.get("_ready") else "否",
            "是" if v.get("_enabled", True) else "否"))
        bits = []
        for f, label in (("corpid", "企业ID"), ("agentid", "AgentId"),
                         ("mode", "模式"), ("proxy", "代理"),
                         ("bot_id", "机器人ID"), ("chat_id", "会话ID")):
            if v.get(f):
                bits.append("%s=%s" % (label, str(v[f])[:40]))
        secret_hit = v.get("secret")
        if isinstance(secret_hit, dict):
            bits.append("密钥=%s" % ("已配" if secret_hit.get("has_value") else "未配"))
        if bits:
            lines.append("      " + " · ".join(bits))
    return "\n".join(lines)


def _tool_credit_rules(_args: Dict[str, Any]) -> str:
    """任务积分规则表（回答「预计收益」类问题的依据）。

    2026-10-15 用户要求建立：此前助手答不了「执行完后预计多少积分收益」，
    因为数据源里 `total_credits` 恒为 null，没有规则可依。
    本工具暴露 gateway/task_credits.py 里**实测得出**的规则。
    """
    try:
        try:
            from gateway import task_credits as _tc
        except ImportError:
            import task_credits as _tc  # type: ignore
        return _tc.describe_rules()
    except Exception as e:
        return "积分规则读取失败：%s" % str(e)[:100]


def _tool_estimate_credits(args: Dict[str, Any]) -> str:
    """按连签天数与剩余任务估算积分收益（区分精确与估算）。

    **回答必须区分口径**：exact 是可精确计算的，estimateRange 是历史统计。
    不得把估算值说成确定值。
    """
    try:
        try:
            from gateway import task_credits as _tc
        except ImportError:
            import task_credits as _tc  # type: ignore
    except Exception as e:
        return "积分估算模块不可用：%s" % str(e)[:100]

    a = args or {}
    streak = a.get("streak_days")
    tasks = a.get("remaining_tasks")
    if isinstance(tasks, str):
        tasks = [t.strip() for t in tasks.replace("，", ",").split(",") if t.strip()]
    if not isinstance(tasks, list):
        tasks = None

    if streak is None and not tasks:
        # 没给参数时，自动从任务进度里取连签与剩余任务
        try:
            d = _http_json(WEBUI_BASE + "/api/wb-daily/accounts", timeout=25.0) or {}
            accs = d.get("accounts") or []
            if accs:
                # 取一个代表性账号的连签（通常所有账号节奏一致）
                first = accs[0]
                streak = first.get("streak") or first.get("streak_days")
                tasks = first.get("rest") or None
        except Exception:
            pass

    try:
        r = _tc.estimate_pending(
            int(streak) if streak not in (None, "") else None, tasks)
    except (TypeError, ValueError):
        r = _tc.estimate_pending(None, tasks)

    lines = ["积分收益估算："]
    if r.get("exactItems"):
        lines.append("  【可精确算】")
        for it in r["exactItems"]:
            lines.append("    · %s" % it)
        lines.append("    · 小计：%s 积分" % _fmt_num(r.get("exact")))
    else:
        lines.append("  【可精确算】暂无（未提供连签天数或已知领奖项）")
    lo, hi = (r.get("estimateRange") or [0, 0])[:2]
    lines.append("  【估算区间】%s ~ %s 积分/天" % (_fmt_num(lo), _fmt_num(hi)))
    lines.append("    · %s" % r.get("estimateNote") or "")
    lines.append("")
    lines.append("注：签到与已知领奖项可精确计算；"
                 "旅行/兑换/礼包等由服务端下发，无法预算，只能用历史区间估算。")
    return "\n".join(lines)


def _search_credential() -> Dict[str, Any]:
    """当前选用的搜索引擎及其配置（base_url + api_key + extra）。

    密钥来源优先级（与 LLM provider 完全一致）：
      1. 配置文件里的 searchProviders[<engine>].api_key（用户在设置页填的）
      2. 环境变量 / .env.local 里的 TAVILY_API_KEY —— 仅 Tavily 生效，
         这是给「部署时已配好 key 的老用户」的向后兼容，
         新用户一律走设置页，不再需要碰环境变量。
    """
    cfg = _load_config()
    ws = str(cfg.get("webSearch") or "off").strip()
    if ws == "off":
        return {"ok": False, "reason": "联网搜索未启用"}
    key = str(cfg.get("searchProvider") or ws or "tavily").strip()
    spec = _SEARCH_BY_KEY.get(key)
    if not spec:
        return {"ok": False, "reason": "未知搜索引擎：%s" % key}
    slot = (cfg.get("searchProviders") or {}).get(key) or {}
    base = str(slot.get("base_url") or "").strip() or spec.get("default_base_url") or ""
    api_key = str(slot.get("api_key") or "").strip()
    # 向后兼容：老部署可能只设了环境变量
    if not api_key and key == "tavily":
        api_key = TAVILY_API_KEY
    extra = slot.get("extra") if isinstance(slot.get("extra"), dict) else {}
    return {
        "ok": True, "key": key, "spec": spec, "base_url": base.rstrip("/"),
        "api_key": api_key, "extra": extra or {},
        "name": spec.get("name") or key,
    }


def _search_http(url: str, payload: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None, timeout: int = 20
                 ) -> Optional[Dict[str, Any]]:
    """统一 HTTP 出口：POST JSON 或 GET，返回解析后的 dict（失败返回 None）。"""
    import urllib.request
    import urllib.error
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h,
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return None
    except Exception:
        return None


def _search_adapt_tavily(cr: Dict[str, Any], query: str, n: int,
                         topic: str) -> Optional[Dict[str, Any]]:
    d = _search_http(cr["base_url"] + "/search",
                     {"api_key": cr["api_key"], "query": query, "max_results": n,
                      "topic": topic, "include_answer": True})
    if not isinstance(d, dict):
        return None
    return {"answer": d.get("answer"), "results": d.get("results")}


def _search_adapt_serper(cr: Dict[str, Any], query: str, n: int,
                         topic: str) -> Optional[Dict[str, Any]]:
    endpoint = "/news" if topic == "news" else "/search"
    d = _search_http(cr["base_url"] + endpoint,
                     {"q": query, "num": n, "hl": "zh-CN"},
                     {"X-API-KEY": cr["api_key"]})
    if not isinstance(d, dict):
        return None
    results = []
    for it in (d.get("organic") or [])[:n]:
        if isinstance(it, dict):
            results.append({"title": it.get("title"), "url": it.get("link"),
                            "content": it.get("snippet")})
    ans = d.get("answerBox") or {}
    answer = ans.get("answer") or ans.get("snippet") if isinstance(ans, dict) else None
    return {"answer": answer, "results": results}


def _search_adapt_bing(cr: Dict[str, Any], query: str, n: int,
                       topic: str) -> Optional[Dict[str, Any]]:
    import urllib.parse
    qs = urllib.parse.urlencode({"q": query, "count": n, "mkt": "zh-CN"})
    d = _search_http(cr["base_url"] + "/search?" + qs, None,
                     {"Ocp-Apim-Subscription-Key": cr["api_key"]})
    if not isinstance(d, dict):
        return None
    results = []
    for it in ((d.get("webPages") or {}).get("value") or [])[:n]:
        if isinstance(it, dict):
            results.append({"title": it.get("name"), "url": it.get("url"),
                            "content": it.get("snippet")})
    return {"answer": None, "results": results}


def _search_adapt_google_cse(cr: Dict[str, Any], query: str, n: int,
                             topic: str) -> Optional[Dict[str, Any]]:
    import urllib.parse
    cx = str(cr["extra"].get("cx") or "").strip()
    if not cx:
        return None
    qs = urllib.parse.urlencode({"key": cr["api_key"], "cx": cx, "q": query,
                                 "num": n})
    d = _search_http(cr["base_url"] + "?" + qs, None, None)
    if not isinstance(d, dict):
        return None
    results = [{"title": it.get("title"), "url": it.get("link"),
                "content": it.get("snippet")}
               for it in (d.get("items") or [])[:n] if isinstance(it, dict)]
    return {"answer": None, "results": results}


def _search_adapt_bocha(cr: Dict[str, Any], query: str, n: int,
                        topic: str) -> Optional[Dict[str, Any]]:
    d = _search_http(cr["base_url"] + "/web-search",
                     {"query": query, "count": n, "summary": True},
                     {"Authorization": "Bearer " + cr["api_key"]})
    if not isinstance(d, dict):
        return None
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    pages = (data or {}).get("webPages") or (data or {}).get("value") or []
    results = [{"title": it.get("name") or it.get("title"), "url": it.get("url"),
                "content": it.get("summary") or it.get("snippet")}
               for it in pages[:n] if isinstance(it, dict)]
    return {"answer": (data or {}).get("answer"), "results": results}


def _search_adapt_searxng(cr: Dict[str, Any], query: str, n: int,
                          topic: str) -> Optional[Dict[str, Any]]:
    import urllib.parse
    qs = urllib.parse.urlencode({"q": query, "format": "json",
                                 "language": "zh-CN", "safesearch": 0})
    d = _search_http(cr["base_url"] + "/search?" + qs, None, None)
    if not isinstance(d, dict):
        return None
    results = [{"title": it.get("title"), "url": it.get("url"),
                "content": it.get("content")}
               for it in (d.get("results") or [])[:n] if isinstance(it, dict)]
    answers = d.get("answers") or []
    answer = answers[0] if answers else None
    return {"answer": answer, "results": results}


_SEARCH_ADAPTERS = {
    "tavily": _search_adapt_tavily,
    "serper": _search_adapt_serper,
    "bing": _search_adapt_bing,
    "google_cse": _search_adapt_google_cse,
    "bocha": _search_adapt_bocha,
    "searxng": _search_adapt_searxng,
}


def _tool_web_search(args: Dict[str, Any]) -> str:
    """联网搜索（搜索引擎可在设置页自选，密钥由用户自行配置）。

    2026-10-16 重构：原先写死 Tavily + 依赖环境变量 TAVILY_API_KEY，
    与「开源项目不该内置他人 key」的诉求冲突。现在：
      - 设置页「联网搜索」下拉选引擎（Tavily / Serper / Bing /
        Google CSE / 博查 / SearXNG）
      - 每个引擎的 base_url 与 API Key 都在设置页填写，
        只落在用户自己的 assistant_config.json，绝不进仓库
      - 环境变量 TAVILY_API_KEY 仍被识别，仅为兼容老部署
    """
    cr = _search_credential()
    if not cr.get("ok"):
        return "%s：请在设置页「智能助手 → 联网搜索」里启用并填写密钥。" % cr.get("reason")
    if cr["spec"].get("supports_api_key") and not cr["api_key"]:
        return "尚未填写 %s 的 API Key：请在设置页「智能助手 → 联网搜索」里补配置。" % cr["name"]
    query = str((args or {}).get("query") or "").strip()[:400]
    if not query:
        return "请提供搜索关键词。"
    try:
        n = int((args or {}).get("max_results") or 5)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(20, n))
    topic = str((args or {}).get("topic") or "general").strip().lower()
    if topic not in ("general", "news", "finance"):
        topic = "general"

    adapt = _SEARCH_ADAPTERS.get(cr["key"])
    if not adapt:
        return "搜索引擎 %s 尚未实现适配器。" % cr["name"]
    try:
        d = adapt(cr, query, n, topic)
    except Exception as e:
        return "联网搜索失败（%s）：%s" % (type(e).__name__, str(e)[:120])
    if not d:
        return "联网搜索失败（%s 未返回可用结果）：请检查设置页里该引擎的地址与密钥。" % cr["name"]

    lines: List[str] = []
    ans = str(d.get("answer") or "").strip()
    if ans:
        lines.append("摘要：%s" % ans[:400])
    for it in (d.get("results") or [])[:n]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()[:80]
        url = str(it.get("url") or "").strip()[:160]
        content = str(it.get("content") or "").strip().replace("\n", " ")[:200]
        lines.append("· %s\n  %s\n  %s" % (title, url, content))
    if not lines:
        return "联网搜索无结果：%s" % query
    return "\n".join(lines)
# ---------------------------------------------------------------------------
# 工具边界说明（2026-10-15 用户确认）
#
# 「不给工具」 ≠ 「禁用接口」。这两件事必须分清，否则后人维护时容易误读：
#
#   · 面板 / WebUI —— 所有接口**完全可用**，登录、改密、导出入、删账号
#     照常操作，一行代码没动。
#   · 助手工具  —— 只是**不给模型一个 function 声明**，使 LLM 无法从
#     自然语言路径触发它。
#
# 为什么区分（触发路径风险不同）：
#   面板点击 → 用户明确、有 UI 确认、看得见自己在改什么 → 安全
#   LLM 工具 → 自然语言触发、可能被提示注入诱导 → 有风险
#   反例：若 change_password 是 LLM 工具，「帮我改成密码 123456」
#        可能被模型真的执行 —— 不可逆且涉账号安全。
#
# 【不给助手工具的接口清单】（接口本身都在，只是无 tool 声明）：
#   认证类 8 个：auth/login、auth/logout、auth/password、auth/username、
#                auth/profile(GET/PUT)、auth/status、auth/avatar-upload、
#                auth/avatar
#   危险操作 4 个：delete（删账号，不可逆）、api-keys/delete-all（一把清空）、
#                import（会覆盖现有账号池）、refresh-token（全量刷新连锁失效）
#   助手自身配置 4 个：assistant(GET/PUT)、assistant/test、assistant/models
#   回调 2 个：notify/wecom/callback(GET/POST)（企微平台调用，非助手职责）
#   配置写入 2 个：notify PUT、wb-daily/config POST
#
# 【已给助手工具的能力】：见下方 _TOOLS 注册表（只读 30 个）
# 与 _WRITE_OPS（写操作 10 个，全部走两段式确认）。
# ---------------------------------------------------------------------------

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
    # ---- 联网搜索（Tavily，2026-10-15 照 MP 的「联网搜索」项新增）----
    "web_search": (_tool_web_search, "联网搜索：用 Tavily 实时检索网页，回答需要最新外部信息的问题",
                   "query: 搜索词；max_results: 结果数，默认 5；topic: general|news，默认 general"),
    # ---- 补全批次 1：任务域 7 个（2026-10-15）----
    "get_task_runs": (_tool_task_runs, "任务轮次历史：每轮的开始/结束/状态/账号数（查「跑过没/跑完没」用这个）",
                      "limit: 返回条数，默认 10；status: 按状态筛选 done|failed|running"),
    "get_task_detail": (_tool_task_detail, "单轮任务详情：失败原因、输出摘要、返回码",
                        "job_id: 轮次 ID（可用 get_task_runs 获取）"),
    "get_task_accounts": (_tool_task_accounts, "某轮任务的账号明细：等级/连续天数/完成进度/未完成任务",
                          "job_id: 轮次 ID"),
    "get_task_catalog": (_tool_task_catalog, "任务目录：有哪些任务、哪些能自动跑、哪些需手动", "无参数"),
    "get_task_config": (_tool_task_config, "任务配置：开关/间隔/运行模式/签到开关/保留天数", "无参数"),
    "get_task_health": (_tool_task_health, "任务服务健康：是否运行中、上次与下次执行时间", "无参数"),
    # ---- 签到活动（2026-10-17 新增）：回答「签到给多少分 / 500 是怎么回事」用实时字段 ----
    "get_checkin_activity": (_tool_checkin_activity,
        "签到活动详情：本期每日积分、连签天数/累计、本周进度、连签奖励、活动周期（回答「签到给多少分」「500 是怎么回事」优先用这个，勿凭规则表硬答）",
        "无参数"),
    "get_task_items": (_tool_task_items, "任务执行明细：每个子任务的最近结果与说明",
                       "limit: 返回条数，默认 12"),
    # ---- 补全批次 2：账号域 + 系统域 4 个（2026-10-15）----
    "get_account_health": (_tool_account_health, "账号一致性自检：凭据侧与模型侧结论并排（慢接口，约 1-2 分钟）",
                           "无参数"),
    "get_account_models": (_tool_account_models, "账号模型覆盖：每个账号能用哪些模型",
                           "account: 可选，指定账号名筛选"),
    "get_token_stats": (_tool_token_stats, "Token 用量：按天统计输入/输出/缓存命中率",
                        "days: 返回最近多少天，默认 7"),
    "get_reqlog": (_tool_reqlog, "请求日志明细：最近请求的模型/账号/耗时/结果",
                   "limit: 返回条数，默认 10"),
    "get_notify_config": (_tool_notify_config, "通知配置详情：各渠道就绪状态与关键参数（密钥只显示是否已配）", "无参数"),
    # ---- 积分规则表（2026-10-15 用户要求建立）----
    "get_credit_rules": (_tool_credit_rules, "任务积分规则：签到/领奖的精确积分值与历史量级（回答「预计收益」用这个，勿用 get_credits 的参数）",
                         "无参数"),
    "estimate_credits": (_tool_estimate_credits, "积分收益估算：按连签天数与剩余任务算「还能拿多少」（区分精确与估算）",
                          "streak_days: 连签天数；remaining_tasks: 未完成任务列表"),
}


def _enabled_tool_names() -> List[str]:
    """当前配置下可用的工具名。

    web_search 仅在「联网搜索已启用 且 所选搜索引擎的密钥就绪」时才注册。
    2026-10-16 起改为多引擎：启用条件由 _search_credential() 统一判定，
    不再写死 Tavily（密钥可由用户在设置页自行配置）。
    """
    names = list(_TOOLS.keys())
    try:
        cr = _search_credential()
    except Exception:
        cr = {}
    if not cr.get("ok"):
        names = [n for n in names if n != "web_search"]
    elif cr.get("spec", {}).get("supports_api_key") and not cr.get("api_key"):
        names = [n for n in names if n != "web_search"]
    return names


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
        "web_search": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词，保持 400 字以内"},
            "max_results": {"type": "integer", "description": "返回结果数，1-20，默认 5"},
            "topic": {"type": "string", "description": "general（默认）或 news（实时新闻）"}}},
        "get_task_runs": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回条数，默认 10"},
            "status": {"type": "string", "description": "按状态筛选：done / failed / running"}}},
        "get_task_detail": {"type": "object", "properties": {
            "job_id": {"type": "string", "description": "轮次 ID"}},
            "required": ["job_id"]},
        "get_task_accounts": {"type": "object", "properties": {
            "job_id": {"type": "string", "description": "轮次 ID"}},
            "required": ["job_id"]},
        "get_task_catalog": {"type": "object", "properties": {}},
        "get_task_config": {"type": "object", "properties": {}},
        "get_task_health": {"type": "object", "properties": {}},
        "get_checkin_activity": {"type": "object", "properties": {}},
        "get_task_items": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回条数，默认 12"}}},
        "get_account_health": {"type": "object", "properties": {}},
        "get_account_models": {"type": "object", "properties": {
            "account": {"type": "string", "description": "可选，指定账号名筛选"}}},
        "get_token_stats": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "返回最近多少天，默认 7"}}},
        "get_reqlog": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回条数，默认 10"}}},
        "get_notify_config": {"type": "object", "properties": {}},
        "get_credit_rules": {"type": "object", "properties": {}},
        "estimate_credits": {"type": "object", "properties": {
            "streak_days": {"type": "integer", "description": "当前签到连签天数（未签到时可不传）"},
            "remaining_tasks": {"type": "array", "items": {"type": "string"},
                                "description": "未完成任务名/code 列表"}}},
    }
    enabled = set(_enabled_tool_names())
    return [{"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": props.get(name) or {"type": "object", "properties": {}},
    }} for name, (_fn, desc, _p) in _TOOLS.items() if name in enabled]


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
    # ---- 补全批次 3：新增写操作（2026-10-15）----
    if t in ("发送测试通知", "测试通知", "notify_test"):
        return "notify_test"
    if t in ("注册菜单", "创建菜单", "刷新菜单", "notify_menu"):
        return "notify_menu"
    if t in ("账号巡检", "账号自检", "跑一次账号巡检", "account_audit"):
        return "account_audit"
    if t in ("网关自检", "跑一次自检", "gateway_selftest"):
        return "gateway_selftest"

    # 动词组合（自然语言下令，但不是疑问句）
    if ("任务" in t or "每日" in t) and any(v in t for v in ("执行", "启动", "跑", "运行", "来一轮", "跑一轮")):
        return "run_daily"
    if "签到" in t and any(v in t for v in ("执行", "去", "来", "全部")):
        return "checkin_all"
    if "巡检" in t and any(v in t for v in ("触发", "执行", "跑", "立即")):
        return "probe_models"
    if "测试通知" in t or ("通知" in t and "测试" in t):
        return "notify_test"
    if "菜单" in t and any(v in t for v in ("注册", "创建", "刷新", "更新")):
        return "notify_menu"
    if "自检" in t and any(v in t for v in ("网关", "跑", "执行", "来一次")):
        return "gateway_selftest"
    if "账号" in t and any(v in t for v in ("自检", "巡检", "一致性")):
        return "account_audit"
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


# ---- 补全批次 3：新增写操作（2026-10-15）----

@_register_write_op("notify_test", "将向已启用渠道发送一条测试通知",
                    "所有已启用渠道（会真实发出消息）", "立即")
def _op_notify_test(_args: Dict[str, Any]) -> str:
    try:
        try:
            from gateway import notify as _nt
        except ImportError:
            import notify as _nt  # type: ignore
        cfg = _nt._load()
        sent = []
        for ch in ((cfg.get("channels") or {})).keys():
            ch_cfg = (cfg.get("channels") or {}).get(ch) or {}
            if not ch_cfg.get("_enabled", True):
                continue
            sender = _nt._SENDERS.get(ch)
            if not sender:
                continue
            try:
                r = sender(ch_cfg, "AutoBuddy 测试通知（来自智能助手）")
                sent.append("%s:%s" % (ch, "✅" if r.get("ok") else "❌" + str(r.get("error"))[:60]))
            except Exception as e:
                sent.append("%s:异常 %s" % (ch, str(e)[:40]))
        if not sent:
            return "没有已启用的通知渠道，未发送。"
        return "测试通知已发送：\n  " + "\n  ".join(sent)
    except Exception as e:
        return "操作失败：%s" % str(e)[:100]


@_register_write_op("notify_menu", "将向企微后台注册/刷新自定义菜单",
                    "企微应用的自定义菜单（会覆盖现有菜单）", "立即")
def _op_notify_menu(_args: Dict[str, Any]) -> str:
    try:
        try:
            from gateway import notify as _nt
        except ImportError:
            import notify as _nt  # type: ignore
        d = _http_json(WEBUI_BASE + "/api/notify/menu", timeout=30.0,
                       method="POST", body=b"{}") or {}
        if d.get("error"):
            return "菜单注册失败：%s" % str(d["error"])[:160]
        return "✅ 菜单已注册/刷新：%s" % (d.get("detail") or d.get("status") or "完成")
    except Exception as e:
        return "操作失败：%s" % str(e)[:120]


@_register_write_op("account_audit", "将对全部账号做一次凭据+模型一致性自检",
                    "全部账号（只读探测，不改配置）", "约 1-2 分钟")
def _op_account_audit(_args: Dict[str, Any]) -> str:
    try:
        d = _http_json(WEBUI_BASE + "/api/account-health/audit", timeout=150.0) or {}
    except Exception as e:
        return "自检未完成：%s" % str(e)[:100]
    rows = d.get("rows") or d.get("accounts") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not rows:
        return "自检已执行，但未返回账号明细（可在面板查看详情）。"
    bad = [r for r in rows if isinstance(r, dict)
           and any(str(r.get(k) or "").lower() in ("fail", "failed", "异常", "失效")
                   for k in ("credential", "model", "credentialStatus", "modelStatus"))]
    return "✅ 账号自检完成：共 %d 个，发现 %d 个异常%s" % (
        len(rows), len(bad),
        ("。\n异常账号：" + "、".join(str(r.get("name") or r.get("account")) for r in bad[:8])) if bad else "。")


@_register_write_op("gateway_selftest", "将从容器内实测一次网关连通性",
                    "只读探测（真实发一次请求）", "约 5-15 秒")
def _op_gateway_selftest(_args: Dict[str, Any]) -> str:
    try:
        d = _http_json(WEBUI_BASE + "/api/gateway-selftest", timeout=60.0,
                       method="POST", body=b"{}") or {}
    except Exception as e:
        return "自检失败：%s" % str(e)[:120]
    ok = d.get("ok")
    parts = ["✅ 网关自检：%s" % ("通过" if ok else "异常")]
    for k in ("status", "model", "account", "durationMs", "detail", "error"):
        if d.get(k) is not None:
            parts.append("  · %s：%s" % (k, str(d[k])[:140]))
    return "\n".join(parts)


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


def assistant_reply(session_id: str, question: str,
                    timeout: Optional[float] = None) -> str:
    """自然语言 → （可选工具调用）→ 中文回答。

    ``timeout``：本次调用的总超时（秒）。None = 用 ASSISTANT_TIMEOUT（45s，
    本地/Telegram 同步等）。企微回调会传一个短预算（如 3.5s）先探一下：
    能快出结果就同步答，不给用户「请稍候」。

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
    # 总预算（deadline 制）：``timeout`` 是**整条链路**的预算，不是每轮。
    # 为什么必须这样（2026-10-15 实测）：LLM 是两轮链路（第1轮出工具调用
    # → 执行工具 → 第2轮出答案）。之前把 _tmo 当每轮超时用，两轮各 3.5s
    # 叠加 → 实际 5.4s，越过了企微 5s 硬上限。改成算好绝对截止时间，
    # 每轮调用前用剩余时间，总耗时才不会超预算。
    _tmo = float(timeout) if timeout else ASSISTANT_TIMEOUT
    _deadline = time.time() + _tmo

    def _remaining() -> float:
        """距截止还剩多少秒（<0 表示已超）。"""
        return _deadline - time.time()
    base = rt.get("base_url") or GATEWAY_BASE
    api_key = rt.get("api_key") or ""
    sys_prompt = rt.get("systemPrompt") or SYSTEM_PROMPT
    # 啰嗦模式 / 输出语言：作为追加指令拼到 system prompt 末尾，
    # 不污染用户自定义的 systemPrompt 本体。
    _extra: List[str] = []
    if rt.get("verbose"):
        _extra.append("回复可以稍详细：先给结论，再附关键依据与建议。")
    else:
        _extra.append("回复简洁：直接给结论，避免多余铺垫与重复。")
    _lang = str(rt.get("outputLanguage") or "").strip()
    if _lang and _lang.lower() not in ("", "auto"):
        _lang_cn = {"zh-CN": "简体中文", "zh-TW": "繁体中文", "en": "English",
                    "ja": "日本語", "ko": "한국어"}.get(_lang, _lang)
        _extra.append("始终使用%s回复。" % _lang_cn)
    if _extra:
        sys_prompt = sys_prompt + "\n\n【本次会话要求】" + "".join(_extra)

    messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
    messages.extend(_history(session_id))
    messages.append({"role": "user", "content": q[:1500]})

    tools = _tool_registry()
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    # 温度：配置为空则**不传**（用模型默认）；配了才传。
    # 注意 "0" 是合法值，不能用 `if rt.get("temperature")` 判真假。
    if rt.get("temperature") is not None:
        payload["temperature"] = rt["temperature"]
    # 思考深度：只在非 auto 时显式下发，避免不支持该参数的网关报错。
    _effort = str(rt.get("reasoningEffort") or "").strip()
    if _effort and _effort != "auto":
        payload["reasoning_effort"] = _effort
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
        # User-Agent：留空则用 Python 默认，不强行覆盖。
        if rt.get("userAgent"):
            req.add_header("User-Agent", str(rt["userAgent"]))
        _left = _remaining()
        if _left < 0.5:
            return ""                      # 预算耗尽，不发起请求
        with urllib.request.urlopen(req, timeout=min(_tmo, _left)) as r:
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
        # 第二轮：拿工具结果换一句人话回答。
        #
        # ⚠️ 2026-10-17 修复（用户反馈「问了两次都答非所问」）：
        # 实测 step-5-preview 在 tool_choice=none 下**仍会返回 tool_calls**
        # 且 content 为空（三次抽样两次中招：finish_reason=tool_calls，
        # 正文全跑进 reasoning_content）。旧代码只在「请求抛异常」时才
        # 退回无 tools 重试；模型规规矩矩返回 tool_calls 不算异常，
        # 于是 final 一直是空 → 走工具原文回落 → 用户看到一堆未经解读的
        # 字段堆砌，正是「答非所问」的观感。
        #
        # 现在按顺序试两种 payload，谁先给出「非空正文且不再调工具」就用谁：
        #   ① 带 tools + tool_choice=none（首选，上下文里仍有工具声明）
        #   ② 不带 tools（彻底断掉它再调工具的念头）
        final = ""
        for _variant in ("none", "bare"):
            payload2 = dict(payload)
            payload2["messages"] = messages
            if _variant == "none":
                payload2["tool_choice"] = "none"
            else:
                payload2.pop("tools", None)
                payload2.pop("tool_choice", None)
            _left = _remaining()
            if _left < 0.5:
                break                       # 预算耗尽，别再发请求
            try:
                req2 = urllib.request.Request(
                    base + "/v1/chat/completions",
                    data=json.dumps(payload2).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"})
                if api_key:
                    req2.add_header("Authorization", "Bearer %s" % api_key)
                with urllib.request.urlopen(req2, timeout=min(_tmo, _left)) as r2:
                    d2 = json.loads(r2.read().decode("utf-8", "replace"))
            except Exception:
                continue                     # 这个变体不行，换下一个
            msg2 = (d2.get("choices") or [{}])[0].get("message") or {}
            if msg2.get("tool_calls"):
                continue                     # 还在调工具 → 换下一个变体
            cand = (msg2.get("content") or "").strip()
            # 回吐工具调用原文 → 无效输出，继续换变体
            if cand and not _is_tool_echo(cand):
                final = cand
                break
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
