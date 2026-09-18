# WorkBuddy Switch (Docker & OpenAI API Gateway)

<p align="center">
  <img src="./icon.png" width="128" height="128" alt="WorkBuddy Switch Logo" />
</p>

统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号、积分和签到状态，并自带 OpenAI 兼容格式 API 网关。

## 功能特性

- **账号管理与自动保活**：支持 Google 国际版与微信国内版扫码/导入，自动打卡与 Token 刷新保活
- **OpenAI 兼容 API 网关**：支持将上游模型（`hy4`, `deepseek-v3`, `kimi-k3`, `hy3`）转为标准 OpenAI `/v1/chat/completions` 接口
- **自动双向流/非流转换**：无缝对接 Sub2API、OpenClaw、DSH、NextChat 等下游客户端
- **WebUI 统一面板**：提供直观的账号与配额管理控制台

## 端口说明

- `18090`：WebUI 控制面板
- `18091`：OpenAI 兼容 API 接口 (`http://<HOST>:18091/v1`)
