> 交互加入智能助手：自然语言提问，实时数据回答（参考 MoviePilot 的 agent/llm 设计）。

## 需求

用户要求「给交互加入智能助手，以实现更多功能，也可以参考 MoviePilot 实现」。

## 参考 MoviePilot 的 app/agent/llm/

| MoviePilot | 本实现对应 |
|---|---|
| `tools.py` 工具注册表（能力声明 + 解析） | `assistant._TOOLS` 注册表 + `_tool_registry()` |
| 明确的 tool 边界 | 每个 tool 独立 try/except，异常转可读文本 |
| 会话隔离 | 按 `FromUserName`/`chat_id` 分会话，TTL 过期 |
| 命令与自然语言分层 | **命令优先，LLM 兜底** |

## 设计取舍

1. **命令优先，LLM 兜底**：9 个既有命令是确定性路径（零延迟、不依赖模型可用性），
   永不被 LLM 故障拖累；助手只接管「未命中命令」的自然语言。
2. **复用自家网关供电**：助手经 loopback（127.0.0.1:18091）调自家网关，
   `_gateway_auth` 对 loopback 免鉴权，无需额外密钥；天然继承账号池调度、
   冷却、配额 —— 不占额外资源，也不受单账号限流影响。
3. **只读工具**：助手不执行写操作（启动任务/签到/开关通知），
   避免自然语言歧义导致误操作；写操作仍走显式命令。
4. **可一键关闭**：`AB_ASSISTANT_ENABLED=0` 立即退回纯命令模式。
5. **绝不阻塞事件循环**：web_proxy 回调用 `run_in_threadpool` 跑（LLM 最长 45s）。

## 能力

- **6 个只读工具**：积分统计、账号余额、当前模型、网关状态、任务进度、请求指标
- **多轮对话**：按用户分会话，保留最近 6 轮，TTL 30 分钟
- **模型自动挑选**：按 hy3 → deepseek-v3 → kimi-k3 → deepseek-chat 优先级，
  也可用 `AB_ASSISTANT_MODEL` 指定
- **菜单新增「智能助手」按钮**（`AB_ASSISTANT`），「助手」命令查看用法

用法示例（企微/Telegram 直接发）：
```
现在积分多少？
哪个账号余额最高？
模型有哪些能用？
今天任务跑完了吗？
```

## 测试

新增 `_test_assistant.py`（64 项）：

- 模块契约：开关/超时/TTL/会话上限/输出封顶/loopback 供电；
- 工具集：6 工具注册、OpenAI function 格式、异常隔离、
  **全部只读**（无 POST/PUT，启动任务与签到不在工具集）；
- 集成契约：`_assistant_fallback`、三个调用点透传 session_id
  （企微 from_user / Telegram chat_id）、菜单按钮与映射、
  help 文本说明、不可用回落；
- **web_proxy 无第二份助手实现**（规避 v0.9.63「两份代码改一半」的教训）；
- 真实模块行为：`is_command` 10 个用例、会话隔离、TTL 清理、
  工具异常隔离、关闭开关返回空串。

全量 **39 个测试文件全绿**。

## 新增可调参数

| 变量 | 默认 | 作用 |
|---|---|---|
| `AB_ASSISTANT_ENABLED` | 1 | 助手总开关 |
| `AB_ASSISTANT_MODEL` | (空=自动) | 指定助手模型 |
| `AB_ASSISTANT_TIMEOUT` | 45 | LLM 超时（秒） |
| `AB_ASSISTANT_SESSION_TTL` | 1800 | 会话 TTL（秒） |
| `AB_ASSISTANT_MAX_SESSIONS` | 200 | 最大会话数 |
| `AB_ASSISTANT_HISTORY` | 6 | 保留历史轮数 |

## 升级说明

重建容器即可。`/data` 数据不受影响；助手默认开启，
如需关闭在容器环境变量加 `AB_ASSISTANT_ENABLED=0`。
