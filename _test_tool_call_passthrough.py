#!/usr/bin/env python3
"""工具调用透传回归测试。

背景（用户 2026-10-02 反馈）：
「所有模型都返回 finish_reason=tool_calls 却不携带 tool_calls 数组」。

根因在非流式路径：网关把上游流式帧重组成一条 assistant message 时，
**只挑了 delta 里的 content**，其余字段（tool_calls / reasoning_content）
被静默丢弃。于是客户端拿到：

    finish_reason = "tool_calls"
    message       = {"role": "assistant", "content": ""}
    tool_calls    = None          ← 没有

模型「说要调工具」，但调用内容一个字都没传下去 —— Agent 侧的
function calling 链路整条断掉，且**不报错**（空 content 看起来像正常回复），
排查时极容易被误判成「模型不支持工具」。

这里锁住两条契约：

1. ``accumulate_delta()`` 能把**逐片下发**的 tool_calls 拼回完整调用；
2. 非流式组装出的 message **原样带上** tool_calls 等字段（真·透传）。

不 import gateway/main.py —— 它依赖 fastapi 等运行时包，本地/CI 未必装了，
而这里校验的是纯函数逻辑，直接从源码里取出该函数执行即可。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
MAIN = ROOT / "gateway" / "main.py"

SRC = MAIN.read_text(encoding="utf-8")

# 从源码里抠出 accumulate_delta 的函数体并就地执行，拿到真实实现。
# 这样测试断言的是**线上跑的那份代码**，而不是抄一份影子实现。
_m = re.search(
    r"^def accumulate_delta\(msg: Dict\[str, Any\], delta: Dict\[str, Any\]\) -> None:.*?(?=\n@app\.|^@app\.|^# -{10,}|\Z)",
    SRC,
    re.S | re.M,
)
if not _m:
    print("❌ gateway/main.py 里找不到 accumulate_delta")
    raise SystemExit(1)

_ns: dict = {"Dict": dict, "Any": object}
exec(_m.group(0), _ns)
accumulate_delta = _ns["accumulate_delta"]


_ok = 0
_fail: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra else ""))


print("[1] 逐片下发的 tool_calls 必须拼回完整一条")

msg: dict = {}
# 上游典型切片：首帧给 id + 函数名，之后每次只来几个字符的 JSON 片段
pieces = [
    {"index": 0, "id": "call_abc", "type": "function",
     "function": {"name": "get_time", "arguments": ""}},
    {"index": 0, "function": {"arguments": '{"tz"'}},
    {"index": 0, "function": {"arguments": ':"Asia/'}},
    {"index": 0, "function": {"arguments": 'Shanghai"}'}},
]
for p in pieces:
    accumulate_delta(msg, {"role": "assistant", "tool_calls": [p]})

calls = msg.get("tool_calls") or []
check("只产生一条 tool_call（不是每片一条）", len(calls) == 1, f"got {len(calls)}")
if calls:
    check("arguments 被拼成完整 JSON",
          calls[0]["function"]["arguments"] == '{"tz":"Asia/Shanghai"}',
          f'got {calls[0]["function"]["arguments"]!r}')
    check("id 保留", calls[0].get("id") == "call_abc", f'got {calls[0].get("id")}')
    check("函数名保留", calls[0]["function"].get("name") == "get_time")
    try:
        json.loads(calls[0]["function"]["arguments"])
        check("拼出来的 arguments 是合法 JSON", True)
    except Exception as exc:
        check("拼出来的 arguments 是合法 JSON", False, str(exc))

print("\n[2] 多个工具并行调用要按 index 各归各位")

msg2: dict = {}
accumulate_delta(msg2, {"role": "assistant", "tool_calls": [
    {"index": 0, "id": "c0", "function": {"name": "a", "arguments": "{"}},
]})
accumulate_delta(msg2, {"tool_calls": [
    {"index": 1, "id": "c1", "function": {"name": "b", "arguments": "["}},
]})
accumulate_delta(msg2, {"tool_calls": [
    {"index": 0, "function": {"arguments": '"x":1}'}},
]})
accumulate_delta(msg2, {"tool_calls": [
    {"index": 1, "function": {"arguments": '1]'}},
]})
c = msg2.get("tool_calls") or []
check("两条调用并存", len(c) == 2, f"got {len(c)}")
if len(c) == 2:
    check("index 0 拼对", c[0]["function"]["arguments"] == '{"x":1}',
          f'got {c[0]["function"]["arguments"]!r}')
    check("index 1 拼对", c[1]["function"]["arguments"] == "[1]",
          f'got {c[1]["function"]["arguments"]!r}')

print("\n[3] content / reasoning 累加，role 首帧落定")

msg3: dict = {}
accumulate_delta(msg3, {"role": "assistant", "content": "你好"})
accumulate_delta(msg3, {"content": "，世界"})
accumulate_delta(msg3, {"reasoning_content": "想"})
accumulate_delta(msg3, {"reasoning_content": "一想"})
check("content 累加", msg3.get("content") == "你好，世界", f'got {msg3.get("content")!r}')
check("reasoning_content 累加", msg3.get("reasoning_content") == "想一想",
      f'got {msg3.get("reasoning_content")!r}')
check("role 保留", msg3.get("role") == "assistant")

print("\n[4] 无 tool_calls 时不得凭空造出空数组")

msg4: dict = {}
accumulate_delta(msg4, {"role": "assistant", "content": "普通回复"})
check("纯文本回复不带 tool_calls 键", "tool_calls" not in msg4, f"got {msg4}")

print("\n[5] 非流式组装把 tool_calls 放进最终 message（真正的透传）")

# 线上那段组装逻辑：dict(assistant_msg) -> 补 role -> content 兜底 -> 丢给 choices
body = re.search(
    r"        final_msg = dict\(assistant_msg\).*?\n(\s+)result_payload = \{",
    SRC,
    re.S,
)
check("源码中存在 final_msg 组装段（不再只塞 content）", body is not None)

check("组装段把 final_msg 放进 message（不是内联 content）",
      '"message": final_msg' in SRC,
      "源码里找不到 '\"message\": final_msg'")

# 端到端复现：把修复后应得的 message 拼出来，对照用户报的故障形态
final = dict(msg)
final["role"] = final.get("role") or "assistant"
final["content"] = final.get("content") if final.get("content") is not None else ""
check("组装结果带 tool_calls（修好了用户报的现象）", bool(final.get("tool_calls")))
check("组装结果 role 为 assistant", final.get("role") == "assistant")

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
