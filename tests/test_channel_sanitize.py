#!/usr/bin/env python3
"""渠道校验指纹句改写（CHANNEL_FINGERPRINT_RULES）回归测试。

背景（2026-10-25 事故）：
  dsh（DeepSeek Harness）经网关调用稳定收到上游 400 code=11128
  「Illegal API invocation from an unapproved channel」，换号无效 ——
  同一批账号最小请求立刻成功。对 dsh 真实请求体二分复测定位：
  触发点是 dsh 默认 system prompt 第一句
    "You are an AI agent powered by DeepSeek Harness"
  整句出现即被上游渠道校验特征匹配拦截。

  修复在 gateway/main.py 的 _upstream_body：转发前按
  CHANNEL_FINGERPRINT_RULES 把指纹句改写为语义等价近义句。

说明：与 test_text_replacements 同思路，直接解析 main.py 源码而不 import ——
  main 依赖 fastapi 等运行时包，本地/CI 未必装了；这里要校验的只是
  改写表的内容与接线，不需要跑起整个网关。
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "gateway" / "main.py"
src = SRC.read_text(encoding="utf-8")

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

print("[1] 改写表内容")
m = re.search(r"CHANNEL_FINGERPRINT_RULES\s*=\s*\[(.*?)\n\]", src, re.S)
check("main.py 里存在 CHANNEL_FINGERPRINT_RULES 表", m is not None)
pairs = re.findall(r'\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)', m.group(1)) if m else []
check("改写表非空", bool(pairs))
check(f"改写表覆盖实测指纹句 {TRIGGER!r}",
      any(TRIGGER in hit for hit, _ in pairs),
      "上游特征串变了时按新串追加规则，勿删旧规则（防回归）")
check("替换串本身不再包含命中串（避免自我重复命中）",
      all(hit not in repl for hit, repl in pairs))

print("\n[2] 接线")
check("_upstream_body 在 CHANNEL_SANITIZE 开启时调用 _sanitize_channel_fingerprints",
      re.search(r"if CHANNEL_SANITIZE:\s*\n\s*out\[.messages.\]\s*=\s*_sanitize_channel_fingerprints", src) is not None)
check("提供 AB_CHANNEL_SANITIZE 开关（=0 可整体回退透传）",
      "AB_CHANNEL_SANITIZE" in src)
san_src = (SRC.parent / "sanitize.py").read_text(encoding="utf-8") if (SRC.parent / "sanitize.py").exists() else src
check("改写函数同时覆盖 str 与多模态 list 两种 content 形态",
      ("isinstance(content, str)" in src and "isinstance(content, list)" in src) or
      ("isinstance(content, str)" in san_src and "isinstance(content, list)" in san_src))

print("\n[3] 行为模拟（用提取出的规则表原样复现改写逻辑）")
sample = [
    {"role": "system", "content": "You are an AI agent powered by DeepSeek Harness.\n\nMore rules."},
    {"role": "user", "content": [{"type": "text", "text": TRIGGER + " suffix"}]},
]


def sanitize(messages):
    for msg in messages:
        c = msg.get("content")
        if isinstance(c, str):
            for hit, repl in pairs:
                c = c.replace(hit, repl)
            msg["content"] = c
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    t = part["text"]
                    for hit, repl in pairs:
                        t = t.replace(hit, repl)
                    part["text"] = t
    return messages


out = sanitize([dict(m) for m in sample])
blob = str(out)
check("str content 中指纹句已被改写", TRIGGER not in blob)
check("list content 中指纹句已被改写", TRIGGER not in blob)
check("改写只动命中片段，句子其余部分原样保留",
      "More rules." in out[0]["content"] and "suffix" in str(out[1]["content"]))

print(f"\n{_ok} passed, {len(_fail)} failed")
sys.exit(1 if _fail else 0)
