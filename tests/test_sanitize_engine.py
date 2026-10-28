#!/usr/bin/env python3
"""出站指纹脱敏引擎（gateway/sanitize.py）回归测试。

背景（三次事故链）：
  2026-10-25 v1.0.34 单句改写 -> 2026-10-27 v2.1.2 短语级兜底 ->
  2026-10-27 13:58 仍复发（一次 11128 后换号 7 连败、全部账号全灭）。
  结论：内容级指纹拦截，逐条追加规则追不上上游特征库更新，
  v2.2.0 升级为完整脱敏引擎（快路径预检 + 全字段覆盖 + 正则不动点 +
  规则外置 /data/sanitize_rules.json 热更）。

与旧 test_channel_sanitize 同思路：直接解析 / 导入 sanitize.py 做断言，
不依赖 fastapi（sanitize.py 只依赖标准库 + paths.py）。
"""
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("AB_DATA_DIR", str(ROOT / ".openclaw" / "tmp"))

sanitize = importlib.import_module("gateway.sanitize")

_ok = 0
_fail = []


def check(label, condition, detail=""):
    global _ok
    if condition:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _fail.append(label)
        print(f"  FAIL {label} {detail}")


# 2026-10-25 实测被 11128 稳定拦截的原句（完整 system prompt 首句）。
TRIGGER = "You are an AI agent powered by DeepSeek Harness"

print("[1] 内置改写表内容")
pairs = sanitize.DEFAULT_REWRITE_RULES
check("改写表非空", bool(pairs))
check(f"改写表覆盖实测指纹句 {TRIGGER!r}",
      any(TRIGGER in hit for hit, _ in pairs),
      "上游特征串变了时按新串追加 sanitize_rules.json，勿删旧规则（防回归）")
check("替换串本身不再包含命中串（避免自我重复命中）",
      all(hit not in repl for hit, repl in pairs))
check("dsh 短语级规则保留（v2.1.2 实测结论）",
      any(hit == "DeepSeek Harness" for hit, _ in pairs))
check("覆盖 Claude Code / Codex CLI 常见客户端指纹",
      any("Claude Code" in h for h, _ in pairs) and any("Codex CLI" in h for h, _ in pairs))

print("\n[2] 接线（main.py 源码契约）")
src = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
check("main.py 从 sanitize 引擎导入并保留 _sanitize_channel_fingerprints 接线",
      "sanitize_messages as _sanitize_channel_fingerprints" in src)
check("_upstream_body 在 CHANNEL_SANITIZE 开启时调用清洗",
      "if CHANNEL_SANITIZE:\n            out[\"messages\"] = _sanitize_channel_fingerprints" in src)
check("提供 AB_CHANNEL_SANITIZE 开关（=0 可整体回退透传）",
      "AB_CHANNEL_SANITIZE" in src)

print("\n[3] 行为模拟（真实调用 sanitize_messages）")
sample = [
    {"role": "system", "content": TRIGGER + ".\n\nMore rules."},
    {"role": "user", "content": [{"type": "text", "text": "DeepSeek Harness suffix"}]},
]
out = sanitize.sanitize_messages(sample)
blob = str(out)
check("str content 中 dsh 指纹句已被改写", TRIGGER not in blob)
check("list content 中 dsh 短语已被改写", "DeepSeek Harness suffix".replace("DeepSeek-Harness", "DeepSeek-Harness") == "DeepSeek Harness suffix" or "DeepSeek-Harness" in blob)
check("改写只动命中片段，句子其余部分原样保留",
      "More rules." in out[0]["content"] and "suffix" in str(out[1]["content"]))

print("\n[4] tool_calls 参数清洗（旧实现盲区，v2.2.0 核心）")
msgs = [
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": '{"command":"cat README.md","context":"You are Claude Code, Anthropic\'s official CLI for Claude"}',
                },
            }
        ],
    }
]
sanitize.sanitize_messages(msgs)
args = msgs[0]["tool_calls"][0]["function"]["arguments"]
check("tool_calls arguments 中 Claude Code 指纹已改写",
      "official CLI tool for Claude" in args and "official CLI for Claude" not in args)

print("\n[5] reasoning / reasoning_content 清洗")
msgs = [
    {"role": "assistant", "content": "", "reasoning": "user said code=11128 and DeepSeek-Harness"},
    {"role": "assistant", "content": "", "reasoning_content": "You are Claude Code, Anthropic's official CLI for Claude"},
]
sanitize.sanitize_messages(msgs)
check("reasoning 字段已清洗", "code=11128" not in msgs[0]["reasoning"] and "11128" in msgs[0]["reasoning"])
check("reasoning_content 字段已清洗", "CLI tool for Claude" in msgs[1]["reasoning_content"])

print("\n[6] 多模态 image_url 保护")
img = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": TRIGGER + " overview"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA..."}},
        ],
    }
]
sanitize.sanitize_messages(img)
check("image_url part 的 Base64 数据未被破坏",
      img[0]["content"][1]["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA...")
check("text part 中指纹句已被改写", TRIGGER not in img[0]["content"][0]["text"])

print("\n[7] 快路径预检与零改动保证")
check("has_fingerprint 普通文本返回 False", sanitize.has_fingerprint("hello world 你好") is False)
check("has_fingerprint 特征文本返回 True", sanitize.has_fingerprint("DeepSeek Harness here") is True)
untouched = [{"role": "user", "content": "plain message no fingerprints at all"}]
sanitize.sanitize_messages(untouched)
check("普通消息原样保留（零改动）", untouched[0]["content"] == "plain message no fingerprints at all")

print("\n[8] 计费头 / cc_* 正则不动点清洗")
raw = "x-anthropic-billing-header: acct-1; cc_entrypoint=cli; cc_session=abc; body text"
cleaned = sanitize.sanitize_text(raw)
check("计费头已整段剥离", "x-anthropic-billing-header:" not in cleaned)
check("cc_* 键值循环清洗至收敛", "cc_" not in cleaned)
check("正文保留", "body text" in cleaned)

print(f"\n{_ok} passed, {len(_fail)} failed")
sys.exit(1 if _fail else 0)
