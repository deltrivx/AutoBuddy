对齐 `CLIProxyAPI-freetier2api-plugin` 的完整协议管线，彻底消除因 `developer` 角色及参数差异导致的上游 `HTTP 400 (11128)` 渠道拦截。

### 修复内容

- **根本病灶与实测验证**：
  - 坐实上游接口严格审计 `role` 白名单（仅允许 `system` / `user` / `assistant` / `tool`）。
  - 现代 OpenAI SDK、DSH 等客户端发出的 `role: "developer"` 会被上游判定为非官方客户端，直接触发 `HTTP 400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}`。
- **协议归一化管线对齐**：
  1. `normalize_messages_for_upstream`：将 `developer` 角色转换为 `system`；多模态 `image_url` 为字符串时规整为 `{"url": ...}` 对象。
  2. `_desktop_headers`：补齐官方出站必备指纹头 `X-CodeBuddy-Request: "1"`。
  3. `_upstream_body`：对齐 `tool_choice` 归一化逻辑（对象提取 `name`，`none` 安全移除 `tools` 避免 11101 错误），并将 `max_completion_tokens` 自动转换为 `max_tokens`。
