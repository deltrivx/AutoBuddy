架构级脱敏引擎上线：快路径预检、全字段（content/reasoning/tool_calls.arguments）覆盖、正则不动点清洗与外置 JSON 热更。

## 升级背景与根因

- 前两次修复（v1.0.34 单句静态改写、v2.1.2 短语级兜底）仍发生 `code=11128` 拦截（2026-10-27 13:58 实测），出现一次 11128 后换号 7 连败、全账号全灭的典型内容级拦截特征；
- 根因分析：dsh 等 Agent 工具客户端在消息中常将 `content` 留空，将敏感特征置于 `tool_calls[].function.arguments` 或思考流 `reasoning` / `reasoning_content` 中，旧逻辑只改写 `content` 留下死角；且静态硬编码表无法抵御上游特征库的不断更新。

## 修复与功能增强

1. **新建脱敏引擎 (`gateway/sanitize.py`)**：
   - **快路径预检 (`has_fingerprint`)**：未包含特征词的请求 $O(N)$ 零正则、零分配极速放行，不增加普通调用开销。
   - **全字段深度覆盖**：递归清洗 `messages[].content`（文本与多模态文本 part，显式忽略 `image_url` 保障图片 Base64 安全）、`reasoning` / `reasoning_content` 以及 `tool_calls[].function.arguments`，根除 Agent 工具调用盲区。
   - **正则不动点循环收敛**：剥离计费头 `x-anthropic-billing-header` 与 `cc_*` 注入键值，循环替换至不动点。
   - **规则外置热更新**：支持读取挂载目录下的 `/data/sanitize_rules.json`（支持 `rewrite`、`regex`、`features`），改动免重新发版。
2. **向下兼容与降级保障**：
   - 保留 `AB_CHANNEL_SANITIZE=0` 整体开关。
   - 保留原有 `CHANNEL_FINGERPRINT_RULES` 符号与 `test_channel_sanitize.py` 契约。
3. **单元测试与回归防护**：
   - 新增 `tests/test_sanitize_engine.py`（22 项全绿），覆盖 Claude Code、Codex CLI、dsh、tool_calls 参数与多模态安全。

## 升级方式

Unraid 模板已锁定 `ghcr.io/deltrivx/autobuddy:latest`，在 Unraid 终端执行原生模板重构或等待 Docker 自动升级即可。
