#!/usr/bin/env python3
"""TEXT_REPLACEMENTS 等长约束回归测试。

背景（v0.9.16 事故）：
  TEXT_REPLACEMENTS 会对 **JS 资源** 做改写，且有**等长**约束 ——
  替换前后字节数必须一致。v0.9.16 把「按本地聚合 Token 从高到低排列。」
  改成「按单次调用词元从高到低排列。」时，Token(5字节) → 词元(6字节)
  少 1 字节，触发：

    ValueError: 等长替换被破坏: ... (43) != ... (42)

  结果 /assets/index-*.js 直接返回 500 → React 不挂载 → 页面全白。

  事故链的特点：AST / node --check / 其它单测**全都通过**，
  只有真打开页面才暴露。所以必须在这里卡死。

说明：直接解析 gateway/web_proxy.py 源码而不 import ——
  web_proxy 依赖 fastapi 等运行时包，本地/CI 未必装了，
  而这里要校验的只是「替换表本身的字节长度」，不需要跑起来。
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).parent / "gateway" / "web_proxy.py"


def load_pairs():
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"TEXT_REPLACEMENTS = \[(.*?)\n\]", src, re.S)
    if not m:
        return None
    return re.findall(r'\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)', m.group(1))


def main() -> int:
    pairs = load_pairs()
    if pairs is None:
        print("❌ 未找到 TEXT_REPLACEMENTS 表")
        return 1
    if not pairs:
        print("❌ TEXT_REPLACEMENTS 为空")
        return 1

    bad = []
    for old, new in pairs:
        lo, ln = len(old.encode("utf-8")), len(new.encode("utf-8"))
        if lo != ln:
            bad.append((old, new, lo, ln))

    if bad:
        print("❌ 以下替换违反等长约束（会让 JS 资源返回 500、页面全白）：")
        for old, new, lo, ln in bad:
            diff = ln - lo
            print(f"   {lo} != {ln} 字节: {old!r} -> {new!r}")
            print(f"      差值 {diff:+d}，需补 {abs(diff)} 个填充字符")
        return 1

    print(f"✅ {len(pairs)} 条替换全部等长")
    for old, new in pairs:
        print(f"   {len(old.encode('utf-8')):>3}B  {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
