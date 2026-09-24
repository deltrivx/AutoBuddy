# AutoBuddy v0.5.10

> 修复临时邮箱创建判定错误，恢复注册流程可启动。

## 变更

### 临时邮箱创建判定修复
- **根因**：`create_email` 仅认可 `success` / `ok` 字段，而实际邮箱服务（grok-mail-worker）
  返回 `{"address", "jwt", "token", "domain"}` 结构，被误判为创建失败，导致注册任务在第一步即中止。
- **修复**：兼容 `address` / `data.address` / `email` / `result.address` / `success` / `ok`
  多种返回结构，并优先采用服务端返回的 `address` 作为权威邮箱地址。
