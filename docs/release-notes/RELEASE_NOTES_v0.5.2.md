# AutoBuddy v0.5.2

> 认证凭据环境变量支持与默认 [密钥] 凭据更新。

## 变更

- 支持通过环境变量 `AUTH_USERNAME` / `AUTH_USER` 与 `AUTH_PASSWORD` / `AUTH_PASS` 显式配置 Web 控制台登录账号与密码。
- 若环境变量未配置，默认用户名和密码均为 **`[密钥]`**。
- 容器启动时若检测到环境变量显式指定密码，会自动同步更新持久化数据库中的管理员凭据。
- Unraid 模板增加 `AUTH_USERNAME` 与 `AUTH_PASSWORD` 两个用户级变量配置项。
