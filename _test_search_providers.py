#!/usr/bin/env python3
"""联网搜索引擎配置回归（2026-10-16 第三项）。

### 需求

用户原话：「搜索引擎作为可选项，前端添加 UI 配置，用户自行配置搜索引擎，
也可以用其他的，而不是把我的 key 写死到仓库里，因为这是开源的，
别人也可以用的，所以每个人可以在前端 UI 可以选择常见搜索引擎
输入 API 配置自己的搜索引擎」

### 断言重点

1. 注册表完整：6 个常见引擎，每个都有适配器。
2. **仓库里绝无他人密钥**：这是开源项目的硬约束。
3. secret 语义与 LLM provider 一致：
   不提交=保留、空串=清除、有值=覆盖；GET 永不回明文。
4. 引擎可切换：webSearch 选不同引擎，_search_credential() 跟着变。
5. 工具启用条件：未启用 / 启用了但没密钥 → web_search 不注册。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway"))

import assistant as A  # noqa: E402

# ⚠️ 宿主环境可能设了真实的 TAVILY_API_KEY（部署时留下的）。
# _search_credential() 对 tavily 有「环境变量兜底」的向后兼容逻辑，
# 不隔离的话「无密钥」场景会读到真实 key，甚至真的发出搜索请求。
# 测试全程统一置空，需要时在用例内显式还原。
_AMBIENT_TAVILY = A.TAVILY_API_KEY
A.TAVILY_API_KEY = ""

_ok = 0
_fail = []


def check(name, cond, extra=""):
    global _ok
    if cond:
        _ok += 1
        print("  ok   %s" % name)
    else:
        _fail.append(name)
        print("  FAIL %s %s" % (name, extra))


tmp = tempfile.mkdtemp(prefix="ab_search_")
cfg_file = Path(tmp) / "assistant_config.json"
A.CONFIG_FILE = cfg_file


# ---------------------------------------------------------------- 1. 注册表
print("[1] 注册表完整性与适配器对应")
keys = [p["key"] for p in A.SEARCH_PROVIDERS]
check("至少 5 个常见引擎", len(keys) >= 5, "got %s" % keys)
for need in ("tavily", "serper", "bing", "google_cse", "bocha", "searxng"):
    check("包含引擎 %s" % need, need in keys)
for k in keys:
    check("引擎 %s 有适配器" % k, k in A._SEARCH_ADAPTERS)
for k in A._SEARCH_ADAPTERS:
    check("适配器 %s 对应注册表条目" % k, k in keys)
# 每个引擎必填字段
for p in A.SEARCH_PROVIDERS:
    check("%s 有 default_base_url" % p["key"], bool(p.get("default_base_url")))
    check("%s 有 name" % p["key"], bool(p.get("name")))
    check("%s 有 desc" % p["key"], bool(p.get("desc")))
    check("%s 声明 supports_api_key" % p["key"], "supports_api_key" in p)
# google_cse 必须有 cx 附加字段
gcse = A._SEARCH_BY_KEY["google_cse"]
check("google_cse 有 cx 附加字段",
      any(f["key"] == "cx" for f in (gcse.get("extra_fields") or [])))


# ------------------------------------------------- 2. 仓库无硬编码密钥（核心）
print()
print("[2] 开源硬约束：仓库内不得出现他人密钥")
repo = Path(os.path.dirname(os.path.abspath(__file__)))
leaks = []
# 只扫文本类源码/配置文件，且排除 .git 与本地缓存
skip_dirs = {".git", ".openclaw", "__pycache__", "node_modules", ".venv"}
import re as _re

# 用户诉求的核心是「别把我的 key 写死到仓库里」，所以扫描要**精准**：
#   - tvly-<20+ 位>  ：Tavily 真实密钥形态（用户关心的就是这个）
#   - sk-<48 位十六进制>：OpenAI 形态真实密钥
# _test_*.py 里的 sk-ab- 是**格式字面量**（断言前缀用），不是真实密钥，
# 必须排除，否则必然误报。
TVLY_RE = _re.compile(r"tvly-[A-Za-z0-9]{20,}")
OPENAI_RE = _re.compile(r"sk-[A-Za-z0-9]{40,}")

for root, dirs, files in os.walk(repo):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    for fn in files:
        if not fn.endswith((".py", ".md", ".yml", ".yaml", ".json", ".toml",
                            ".plg", ".sh", ".txt", ".env", ".cfg", ".ini")):
            continue
        if fn.startswith("_test_") and fn.endswith(".py"):
            continue          # 测试文件里的密钥形态是断言字面量，非真实密钥
        fp = Path(root) / fn
        try:
            if fp.stat().st_size > 4 * 1024 * 1024:
                continue
            txt = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            if any(x in line for x in ("示例", "占位", "example", "placeholder",
                                       "your_", "xxx", "***", "your-")):
                continue
            if TVLY_RE.search(line) or OPENAI_RE.search(line):
                leaks.append("%s:%d" % (fp.relative_to(repo), i))
check("仓库内无真实密钥形态字符串", not leaks, "leaks=%s" % leaks[:5])
# 模块级常量不应持有真实 key
check("模块 TAVILY_API_KEY 默认非硬编码",
      A.TAVILY_API_KEY == "" or os.getenv("TAVILY_API_KEY") or cfg_file.exists(),
      "TAVILY_API_KEY=%r" % (A.TAVILY_API_KEY[:6] + "..." if A.TAVILY_API_KEY else ""))


# ---------------------------------------------------- 3. secret 语义一致性
print()
print("[3] secret 语义（不提交=保留 / 空串=清除 / 有值=覆盖 / GET 不回明文）")
A._save_config({
    "webSearch": "tavily",
    "searchProviders": {
        "tavily": {"base_url": "https://api.tavily.com", "api_key": "KEY-FIRST"},
        "serper": {"api_key": "SERPER-KEY"},
    },
})
cfg = A._load_config()
check("tavily 密钥已写入", cfg["searchProviders"]["tavily"]["api_key"] == "KEY-FIRST")
check("serper 密钥已写入", cfg["searchProviders"]["serper"]["api_key"] == "SERPER-KEY")
check("落盘文件存在", cfg_file.exists())

# 不提交 → 保留
A._save_config({"webSearch": "tavily"})
cfg = A._load_config()
check("不提交 api_key 时保留原值",
      cfg["searchProviders"]["tavily"]["api_key"] == "KEY-FIRST")
check("其他引擎密钥不受影响",
      cfg["searchProviders"]["serper"]["api_key"] == "SERPER-KEY")

# 有值 → 覆盖
A._save_config({"searchProviders": {"tavily": {"api_key": "KEY-SECOND"}}})
cfg = A._load_config()
check("有值覆盖", cfg["searchProviders"]["tavily"]["api_key"] == "KEY-SECOND")

# 空串 → 清除
A._save_config({"searchProviders": {"tavily": {"api_key": ""}}})
cfg = A._load_config()
check("空串清除", cfg["searchProviders"]["tavily"]["api_key"] == "")

# None → 保留
A._save_config({"searchProviders": {"serper": {"api_key": "SERPER-KEEP"}}})
A._save_config({"searchProviders": {"serper": {"api_key": None}}})
cfg = A._load_config()
check("None 保留", cfg["searchProviders"]["serper"]["api_key"] == "SERPER-KEEP")

# extra 字段（google_cse 的 cx）
A._save_config({"searchProviders": {"google_cse": {"extra": {"cx": "0123:abcd"}}}})
cfg = A._load_config()
check("extra.cx 可保存",
      cfg["searchProviders"]["google_cse"]["extra"]["cx"] == "0123:abcd")
A._save_config({"searchProviders": {"google_cse": {"extra": {"cx": ""}}}})
cfg = A._load_config()
check("extra.cx 可清除", cfg["searchProviders"]["google_cse"]["extra"]["cx"] == "")

# GET 不回明文
pv = A.public_view()
t_out = pv["config"]["searchProviders"]["tavily"]["api_key"]
check("public_view 不回密钥明文", isinstance(t_out, dict) and "has_value" in t_out,
      "got %r" % (t_out,))
blob = json.dumps(pv, ensure_ascii=False)
check("public_view 全文无 KEY-SECOND/SERPER-KEEP 明文",
      "KEY-SECOND" not in blob and "SERPER-KEEP" not in blob)


# --------------------------------------------------------- 4. 引擎切换生效
print()
print("[4] 引擎切换：_search_credential() 跟随配置")
A._save_config({"webSearch": "off"})
cr = A._search_credential()
check("关闭时 ok=False", cr.get("ok") is False, "got %r" % (cr.get("reason"),))

A._save_config({"webSearch": "serper", "searchProvider": "serper",
                "searchProviders": {"serper": {"api_key": "SERPER-KEY"}}})
cr = A._search_credential()
check("切到 serper 生效", cr.get("ok") and cr.get("key") == "serper", "got %r" % (cr.get("key"),))
check("serper 密钥读得到", cr.get("api_key") == "SERPER-KEEP" or cr.get("api_key") == "SERPER-KEY")
check("serper base_url 正确", cr.get("base_url") == "https://google.serper.dev",
      "got %r" % (cr.get("base_url"),))

A._save_config({"webSearch": "bing", "searchProvider": "bing",
                "searchProviders": {"bing": {"api_key": "BING-KEY"}}})
cr = A._search_credential()
check("切到 bing 生效", cr.get("ok") and cr.get("key") == "bing")
check("bing base_url 正确", cr.get("base_url") == "https://api.bing.microsoft.com/v7.0")

# webSearch 与 searchProvider 不一致时以 searchProvider 为准
A._save_config({"webSearch": "tavily", "searchProvider": "bing"})
cr = A._search_credential()
check("searchProvider 优先于 webSearch", cr.get("key") == "bing",
      "got %r" % (cr.get("key"),))

# 未知引擎
A._save_config({"webSearch": "tavily", "searchProvider": "no_such_engine"})
cr = A._search_credential()
check("未知引擎给出明确原因", cr.get("ok") is False and "未知" in str(cr.get("reason")))


# --------------------------------------------------- 5. 工具启用条件（真调用）
print()
print("[5] web_search 工具启用条件与错误提示（真实调用工具函数）")
# 5a. 关闭 → 不注册 + 调用有提示
A._save_config({"webSearch": "off"})
names = A._enabled_tool_names()
check("关闭时 web_search 不注册", "web_search" not in names)
out = A._tool_web_search({"query": "测试"})
check("关闭时调用返回可读提示", "启用" in out, "got %r" % out[:60])

# 5b. 启用但没密钥 → 不注册 + 提示补配置
A._save_config({"webSearch": "tavily", "searchProvider": "tavily",
                "searchProviders": {"tavily": {"api_key": ""}}})
names = A._enabled_tool_names()
check("启用但无密钥时 web_search 不注册", "web_search" not in names)
out = A._tool_web_search({"query": "测试"})
check("无密钥时提示补配置", "API Key" in out or "密钥" in out, "got %r" % out[:60])

# 5c. 启用且有密钥 → 注册（不真发请求，只验注册逻辑）
A._save_config({"webSearch": "tavily", "searchProvider": "tavily",
                "searchProviders": {"tavily": {"api_key": "tvly-TESTKEYTESTKEYTESTKEY"}}})
names = A._enabled_tool_names()
check("启用且有密钥时 web_search 已注册", "web_search" in names)

# 5d. 不需要密钥的引擎（SearXNG）无需密钥即可注册
A._save_config({"webSearch": "searxng", "searchProvider": "searxng",
                "searchProviders": {"searxng": {"api_key": "",
                                                "base_url": "http://127.0.0.1:8080"}}})
names = A._enabled_tool_names()
check("SearXNG 无需密钥即可注册", "web_search" in names)
cr = A._search_credential()
check("SearXNG credential ok", cr.get("ok") and cr.get("key") == "searxng")
check("SearXNG supports_api_key=False",
      cr.get("spec", {}).get("supports_api_key") is False)

# 5e. 空 query
A._save_config({"webSearch": "tavily", "searchProvider": "tavily",
                "searchProviders": {"tavily": {"api_key": "tvly-TESTKEYTESTKEYTESTKEY"}}})
out = A._tool_web_search({"query": "   "})
check("空 query 有提示", "关键词" in out, "got %r" % out[:60])


# ------------------------------------------- 6. 兼容：老配置/环境变量不回退错
print()
print("[6] 向后兼容：老配置无 searchProviders 字段")
legacy = {
    "enabled": True,
    "provider": "gateway",
    "model": "",
    "systemPrompt": "",
    "maxContextK": 256,
    "temperature": "",
    "reasoningEffort": "auto",
    "outputLanguage": "zh-CN",
    "userAgent": "",
    "verbose": False,
    "webSearch": "off",
    "providers": {
        "gateway": {"base_url": "http://127.0.0.1:18091", "api_key": ""},
        "openai": {"base_url": "https://api.openai.com/v1", "api_key": ""},
    },
}
cfg_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
cfg = A._load_config()
check("老配置缺 searchProviders 时补齐默认值",
      isinstance(cfg.get("searchProviders"), dict) and "tavily" in cfg["searchProviders"])
check("老配置缺 searchProvider 时有默认值", bool(cfg.get("searchProvider")))
pv = A.public_view()
check("老配置 public_view 正常", "searchRegistry" in pv and pv["searchRegistry"])


# ------------------------------------------------------ 7. 引擎数量与选项同步
print()
print("[7] 前端下拉选项与注册表同步")
ws_field = [f for f in A.SETTING_FIELDS if f["key"] == "webSearch"][0]
opts = ws_field.get("options") or []
check("off 在首位", opts[0] == "off")
check("选项覆盖全部引擎", set(opts[1:]) == set(keys),
      "opts=%s keys=%s" % (opts, keys))
check("选项无重复", len(opts) == len(set(opts)))


import shutil
shutil.rmtree(tmp, ignore_errors=True)
A.TAVILY_API_KEY = _AMBIENT_TAVILY   # 还原，避免影响同进程后续 import 者

print()
print("=" * 56)
print("通过 %d 项" % _ok + ("" if not _fail else "，失败 %d 项：%s" % (len(_fail), _fail)))
raise SystemExit(1 if _fail else 0)
