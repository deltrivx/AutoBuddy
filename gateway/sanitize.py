"""出站请求指纹脱敏与反风控引擎（v2.2.0）。

背景（三次事故链）：v1.0.34 单句改写 -> v2.1.2 短语级兜底 -> 2026-10-27 13:58
仍复发：一次 11128 后换号重试 7 连败（200~400ms 一个、全部账号全灭）。
这是**内容级指纹拦截**（账号级拦截换号即过；内容级换号无用），说明上游
特征库覆盖了规则表之外的新指纹串 —— 逐条追加规则追不上上游更新，结构性
升级为完整脱敏引擎（参考 CLIProxyAPI-freetier2api-plugin 的防御管线）。

设计原则：
1. 零开销预检（Fast-path）：特征关键字表先快速判断，普通消息零正则开销。
2. 最小语义变异：上游审核为字面量匹配，微调单个词破坏字典、保留语义。
3. 正则循环收敛清洗：计费头 / cc_* 跟踪键值剥离至不动点。
4. 全字段覆盖：content / reasoning / reasoning_content / tool_calls.arguments
   （旧实现只洗 content —— tool_calls 消息的 content 常为 null，参数里的
   特征串原样发给上游，正是 dsh 这类 Agent 客户端反复中招的盲区）。
5. 多模态安全：显式跳过 image_url part，绝不碰图片 Base64。
6. 规则外置可热更：sanitize_rules.json 放 /data 挂载，上游换特征串时
   改 JSON 即可，不需要发版。环境变量 AB_CHANNEL_SANITIZE=0 整体回退。
"""

import json
import os
import re
import threading
from typing import Any, Dict, List, Tuple

try:
    from gateway.paths import get_data_dir
except ImportError:
    from paths import get_data_dir

# ---------------------------------------------------------------------------
# 配置与开关
# ---------------------------------------------------------------------------
SANITIZE_ENABLED = (os.getenv("AB_CHANNEL_SANITIZE", "1") or "1").strip().lower() not in ("0", "false", "no")

# 内置默认规则（外置 JSON 缺失/损坏时兜底；JSON 存在时与其合并，JSON 优先）。
# 命中串与替换串的写法沿用 v2.1.2 的实测结论：
#   "You are an AI agent running on DeepSeek-Harness"（powered 版）放行，
#   "DeepSeek-Harness" 改写为带连字符的 "DeepSeek-Harness" 形态 100% 放行。
DEFAULT_REWRITE_RULES: List[Tuple[str, str]] = [
    # --- DeepSeek-Harness（dsh）特征：三次实测的原始拦截点，全部保留防回归 ---
    ('You are an AI agent powered by DeepSeek Harness',
     'You are an AI agent running on DeepSeek-Harness'),
    ('DeepSeek Harness',
     'DeepSeek-Harness'),
    # --- Claude Code 特征（常见 Agent 客户端，上游同样做指纹审计） ---
    ("You are Claude Code, Anthropic's official CLI for Claude",
     "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)",
     "Default branch (you will usually use this for PRs)"),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
     "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    # --- Codex CLI 特征 ---
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    # --- 反射报错防误伤：历史消息复述 11128 错误时剥掉 code= 前缀 ---
    ("code=11128", "11128"),
]

# 正则规则（结构化，便于外置 JSON 扩展）：pattern -> replacement（空串=删除）。
DEFAULT_REGEX_RULES: List[Tuple[str, str]] = [
    # Anthropic 计费头整段剥离
    (r"(?i)x-anthropic-billing-header:[^;\n]*;?\s*", ""),
    # 残留裸 Header 键名降级为缩写（不敢全删时保留可读性）
    (r"(?i)x-anthropic-billing-header", "x-anthropic-billing-hdr"),
    # cc_* 跟踪键值（循环清洗在 sanitize_text 内做，这里只定义单次模式）
    (r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*", ""),
]

# 预检特征关键字表：任一命中才进入深度脱敏。普通消息在这里零开销返回。
DEFAULT_FEATURE_PRECHECK: List[str] = [
    'You are an AI agent powered by DeepSeek Harness',
    'DeepSeek Harness',
    "You are Claude Code",
    "Anthropic's official CLI",
    "Main branch (",
    "claude-code/issues",
    "Codex CLI",
    "x-anthropic-billing-header",
    "cc_",
    "code=11128",
    "11128",
]

_RULES_FILE = str(get_data_dir() / "sanitize_rules.json")
_RULES_MTIME = [0.0]
_RULES_LOCK = threading.Lock()


def _load_rules() -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], List[str]]:
    """合并内置规则与外置 JSON（JSON 优先，5s mtime 缓存热更）。"""
    rewrites: Dict[str, str] = {hit: repl for hit, repl in DEFAULT_REWRITE_RULES}
    regexes: List[Tuple[str, str]] = list(DEFAULT_REGEX_RULES)
    features: List[str] = list(DEFAULT_FEATURE_PRECHECK)
    try:
        mt = os.path.getmtime(_RULES_FILE)
        if mt != _RULES_MTIME[0]:
            with open(_RULES_FILE, "r", encoding="utf-8") as f:
                custom = json.load(f)
            _RULES_MTIME[0] = mt
            # JSON 字段形态与内置表对齐；replace 用 dict 覆盖（同 key 后写赢）
            for hit, repl in (custom.get("rewrite") or []):
                rewrites[str(hit)] = str(repl)
                if str(hit) not in features:
                    features.append(str(hit))
            for pat, repl in (custom.get("regex") or []):
                regexes.append((str(pat), str(repl)))
            for feat in (custom.get("features") or []):
                if str(feat) not in features:
                    features.append(str(feat))
            for hit in rewrites:
                if hit not in features:
                    features.append(hit)
    except FileNotFoundError:
        pass
    except Exception:
        # 外置规则损坏时不拖垮网关：回退内置规则
        pass
    return list(rewrites.items()), regexes, features


# ---------------------------------------------------------------------------
# 预编译正则（内置规则在 import 时编译一次；外置新增按需惰性编译）
# ---------------------------------------------------------------------------
_BUILTIN_COMPILED = [(re.compile(p), r) for p, r in DEFAULT_REGEX_RULES]
_CC_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*")


def has_fingerprint(text: str, features: List[str] = None) -> bool:
    """快路径预检：不含任何特征关键字时直接返回 False，零正则开销。"""
    if not text:
        return False
    feats = features if features is not None else DEFAULT_FEATURE_PRECHECK
    return any(f in text for f in feats)


def sanitize_text(text: str,
                  rewrites: List[Tuple[str, str]] = None,
                  compiled: List[Tuple[Any, str]] = None) -> str:
    """对单段文本执行完整脱敏：语义变异 -> 计费头剥离 -> cc_* 不动点清洗。"""
    if not text:
        return text
    for target, repl in (rewrites if rewrites is not None else DEFAULT_REWRITE_RULES):
        if target in text:
            text = text.replace(target, repl)
    for rex, repl in (compiled if compiled is not None else _BUILTIN_COMPILED):
        text = rex.sub(repl, text)
    # cc_* 键值可能前缀剥离后露出下一层，循环清洗至收敛（不动点）
    prev = ""
    while prev != text:
        prev = text
        text = _CC_KV_RE.sub("", text)
    return text


def _sanitize_one_blob(value: str, features: List[str],
                        rewrites: List[Tuple[str, str]],
                        compiled: List[Tuple[Any, str]]) -> str:
    """带预检的单段文本清洗：先快路径判断，命中才进深度脱敏。"""
    if has_fingerprint(value, features):
        return sanitize_text(value, rewrites, compiled)
    return value


def sanitize_messages(messages: Any) -> Any:
    """对 messages 数组深度清洗（原位修改）。

    覆盖四个字段面：
      · content（str / 多模态 parts，跳过 image_url）
      · reasoning / reasoning_content（思考流复述）
      · tool_calls[].function.arguments（工具参数 —— 旧实现的盲区）
    """
    if not SANITIZE_ENABLED or not isinstance(messages, list):
        return messages
    rewrites, regexes, features = _load_rules()
    compiled = [(re.compile(p), r) for p, r in regexes]

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        # 1. 消息正文
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = _sanitize_one_blob(content, features, rewrites, compiled)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                # 严防破坏图片数据：只洗文本 part
                if part.get("type") == "image_url":
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    part["text"] = _sanitize_one_blob(text, features, rewrites, compiled)

        # 2. 思考流（reasoning / reasoning_content）
        for r_key in ("reasoning", "reasoning_content"):
            r_val = msg.get(r_key)
            if isinstance(r_val, str):
                msg[r_key] = _sanitize_one_blob(r_val, features, rewrites, compiled)

        # 3. 工具调用参数（content 为 null 的消息全靠这里兜住）
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function")
                if isinstance(fn, dict):
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        fn["arguments"] = _sanitize_one_blob(args, features, rewrites, compiled)

    return messages
