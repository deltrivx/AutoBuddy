# WorkBuddy Switch (Enhanced with OpenAI Gateway)

WorkBuddy / CodeBuddy 多账号管理与自动签到保活系统，内置 OpenAI 兼容网关。

- **WebUI (18080)**：WorkBuddy 原生多账号管理、扫码登录、自动打卡签到、积分到期监控、自动轮换。
- **OpenAI API Gateway (18081)**：提供标准 `/v1/chat/completions` 与 `/v1/models` 接口，自动桥接当前激活账号的 Token，供 Sub2API、OpenClaw、DeepSeek Harness 等外部 Agent 直接调用 `hy4`、`claude-3-7-sonnet` 等大模型。

## 运行方式 (Docker)

```bash
docker run -d \
  --name WorkBuddy-Switch \
  --restart unless-stopped \
  -p 18080:18080 \
  -p 18081:18081 \
  -v /mnt/user/appdata/workbuddy-switch/data:/data \
  ghcr.io/deltrivx/workbuddy-switch:latest
```
