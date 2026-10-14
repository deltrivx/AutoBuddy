> 修复智能助手一个真实缺陷：hy3 在第二轮会把工具调用原文当正文回吐（`<tool_calls:xxx>`），用户看到乱码。

## 用户反馈后的自查发现

v0.9.67 上线后做线上端到端验证，发现两类提问表现不同：

```
Q: 现在积分还剩多少？
   (6.5s) - 剩余积分：41,023.50 / - 今日消耗：431.69 ... ✅ 正常

Q: 网关状态怎么样？
   (11.1s) <tool_calls:6124c78e> / <tool_call:6124c78e>get_status   ❌ 回吐原文
```

## 取证

容器内探针直连网关，确认 hy3 对 `tools` 参数的响应**格式完全标准**：

```
tool_calls = [{"id": "chatcmpl-tool-afee1f...", "type": "function",
               "function": {"name": "get_status", "arguments": "{}"}}]
content = ''
finish_reason = tool_calls
```

⇒ 第一轮没问题。问题在**第二轮**：原实现把工具结果喂回去时直接**删掉 `tools`**，
hy3 在这种情况下会把工具调用原文（`<tool_calls:xxx>`）当成正文输出。

## 修复

1. 第二轮改用 `tool_choice="none"`（保留 tools 声明、强制出正文），
   并校验「模型又发起工具调用则不采纳其正文」；
2. `tool_choice="none"` 不被支持时，退回「不带 tools」再试一次（两级降级）；
3. 新增 `_is_tool_echo()` 识别回吐原文，判为无效输出；
4. 仍无可用正文时，回落为**直接给工具结果**（比报错有用）。

## 测试

`_test_assistant.py` 新增 8 项源码契约 + 7 项 `_is_tool_echo` 行为用例
（总数 64 → 77）：

- 源码契约：`tool_choice="none"`、降级重试、不采纳再次工具调用的正文、
  tool_echo 判无效；
- 行为用例：`<tool_calls:xxx>` / `<tool_call:xxx>name` / 带空白前缀 → True；
  正常回答 / HTML 加粗 / 空串 → False。

全量 **39 个测试文件全绿**。

## 升级说明

重建容器即可。`/data` 数据不受影响。
