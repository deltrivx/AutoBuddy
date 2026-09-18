# v0.2.0 - 容器化体验全面升级与全量顶级模型网关支持

## 🚀 重大更新与特性亮点

### 1. WebUI 交互与容器化体验全面升级
- **侧边栏折叠收纳**：新增优雅的侧边栏收起/展开按钮，紧凑模式（68px）自适应主区域排版，并支持 `localStorage` 状态持久化记忆。
- **SPA 路由与设置页面彻底修复**：解决官方底层针对 `/settings`、`/token-stats` 等二级路由返回 `application/octet-stream` 导致页面异常白屏或误触发下载的严重问题，现经 CDP 端到端实测秒级加载与交互。
- **Mac 专属残留深度清理**：彻底消除原属于 macOS 桌面版的无用交互（如「在 Finder 中显示」、「打开完全磁盘访问」提示弹窗等），提供专为 NAS、Linux 与 Docker 定制的纯净 Web 控制台。
- **全链路图标视觉统一**：侧边栏、前端 DOM 与浏览器 Favicon 全面同步 Unraid 官方 512x512 圆角高清图标。

### 2. 全量顶级大模型实测接入与 OpenAI 兼容网关增强
- **25+ 款前沿模型端到端支持**：深入逆向提取官方 `@tencent-ai/codebuddy-code` 最新核心配置，并完成全量模型实测并发探活，解锁：
  - **混元系列**：`hy3`（增强思考推理模型，强化代码与复杂逻辑），兼容 `hy4`、`hunyuan`
  - **DeepSeek 系列**：`deepseek-v3`，兼容 `deepseek-chat`
  - **OpenAI 旗舰系列**：`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.5`（旗舰编码大模型）、`gpt-5.4`、`gpt-5.3-codex`、`gpt-4o`
  - **Google Gemini 系列**：`gemini-3.1-pro`、`gemini-3.5-flash`
  - **智谱 GLM 系列**：`glm-5.3`、`glm-5.2`（1M 超长上下文）
  - **Kimi / 月之暗面**：`kimi-k3`（长程科学推理与前端特化）、`kimi-k2.6`、`kimi-k2.5`
  - **MiniMax 系列**：`minimax-m3`（原生多模态 Agent 协同）
  - **智能工作模式**：`default-model` (Auto)、`fast-model` (Fast)、`balanced-model` (Balanced)、`primary-model` (Primary)、`deep-model` (Deep)
- **协议转换**：完善双向流/非流转换，自动规范前置 System Prompt，完美对接 Sub2API、OpenClaw、NextChat 等下游客户端。

### 3. CLI 自动接入与自动化凭证运维
- 容器启动全自动初始化 CodeBuddy CLI helper 与 settings 配置，无需人工干预即可无缝激活 CLI 状态，告别「未接入 CLI」报警。

---

## 📦 部署指引

```bash
docker run -d \
  --name WorkBuddy-Switch \
  -p 18090:18090 \
  -p 18091:18091 \
  -v /mnt/user/appdata/workbuddy-switch:/data \
  --restart unless-stopped \
  ghcr.io/deltrivx/workbuddy-switch:v0.2.0
```
