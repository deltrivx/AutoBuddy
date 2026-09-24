# AutoBuddy v0.5.4

> GitHub 注册自动打码（CapSolver / 2Captcha）与 WorkBuddy OAuth 自动化接入闭环。

## 变更

### 1. 自动打码平台支持 (Arkose FunCaptcha)
- 在注册过程中自动探测页面的 Arkose/Octocaptcha 验证码。
- 支持对接 CapSolver 与 2Captcha 等主流打码平台，全自动求解并在浏览器页面注入 Token，完成绕过与注册表单提交。

### 2. WebUI 参数配置与数据库持久化
- Web 控制台「账号接入」配置卡片增加打码平台选择、API 密钥及自定义 API 端点输入项。
- 配置通过 SQLite 数据库 `system_config` 表及挂载文件双重持久化，容器重启不丢失。

### 3. WorkBuddy OAuth 自动化绑定落库
- 注册完成后在同一浏览器上下文中自动触发 WorkBuddy 国际版 OAuth 授权，完成点击同意并将账号自动归档入 `accounts.json` 账号池。
