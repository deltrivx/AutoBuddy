# WorkBuddy Switch (Docker & OpenAI API Gateway)

<p align="center">
  <a href="https://github.com/deltrivx/workbuddy-switch">
    <img src="./icon.png" width="120" height="120" alt="WorkBuddy Switch Logo" style="border-radius: 28px; box-shadow: 0 8px 24px rgba(0,0,0,0.12);" />
  </a>
</p>

<p align="center">
  <strong>现代化 WorkBuddy / CodeBuddy 智能账号管理套件与高并发 OpenAI 格式 API 网关</strong>
</p>

<p align="center">
  <a href="https://github.com/deltrivx/workbuddy-switch/releases"><img src="https://img.shields.io/github/v/release/deltrivx/workbuddy-switch?color=blue&label=Release" alt="GitHub release" /></a>
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/Unraid-Compatible-F15A24?logo=unraid&logoColor=white" alt="Unraid Compatible" />
  <img src="https://img.shields.io/badge/FastAPI-Gateway-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/OpenAI_API-Compatible-412991?logo=openai&logoColor=white" alt="OpenAI Compatible" />
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License" />
</p>

---

## 📖 项目简介

`WorkBuddy Switch` 是专为 NAS（Unraid / TrueNAS / 群晖）与服务器容器化环境打造的 WorkBuddy 与 CodeBuddy 统一运维与模型调度套件。

本项目将官方底层核心管理服务、**容器化定制 Web 控制台**与**自研高效 OpenAI API 网关**深度融为一体：
- 彻底解决官方原生程序强依赖 macOS/Windows 桌面环境（Finder、完全磁盘访问、导入本机客户端）等容器化痛点。
- 提供了可收纳折叠的纯净管理面板，支持国内版与国际版账号的热切换、Token 自动保活、实时用量图表与积分查看。
- 内置高可用 API 网关，将上游全系列顶级大模型（GPT-5.6/5.5、Gemini-3.5、DeepSeek-V3、GLM-5.3、Kimi-K3、混元 Hy3 等）无缝转为标准 OpenAI 格式，供 **Sub2API**、**OpenClaw**、**NextChat**、**DSH** 等下游无感知接入。

---

## 🌟 核心特性与优化亮点

- 🖥️ **专为容器深度提纯的 WebUI**：
  - **源码级剔除无效桌面功能**：彻底从字节码切除「导入本机账号」及「权限检测」模块，避免任何无法在 Linux 执行的报错。
  - **侧边栏可折叠收纳**：支持 `220px` 与 `68px` 极简图标模式智能切换，具备 `localStorage` 状态持久化记忆。
  - **纯粹清晰的 Token 统计**：剔除冗余分组 Tab 按钮与「Token 总览」重复标题，自研网关实时统计与云端历史融合引擎，告别空白报表。
  - **全链路图标高清对齐**：侧边栏、Header 与 Favicon 全面同步 Unraid 512×512 官方圆角高清图标。
- 🔄 **全自动保活与 CLI 接入**：
  - 容器启动全自动初始化 CodeBuddy CLI 凭证与 Helper，彻底告别「未接入 CLI」报警。
  - 支持 Google 国际版账号、微信扫码登录与备份文件快速导入导出。
- 🚀 **全量模型 OpenAI 兼容网关**：
  - 支持 **25+ 款主流顶级大模型与工作模式别名** 端到端极速调用。
  - 完美支持 `stream: true` 与 `stream: false` 自动双向流/非流转换。
  - 自动补全系统级 Prompt（`normalize_messages`），保障上游 100% 稳定响应。
- 🔗 **开箱即用对接 Sub2API**：
  - 完美适配 Sub2API 的 `apikey` 鉴权与渠道路由，实现多账号轮询与配额统计。

---

## 🤖 官方全量支持模型目录

| 模型类别 | 模型 ID (`model`) | 别名映射 (`aliases`) | 官方说明与能力特性 |
| :--- | :--- | :--- | :--- |
| **混元系列** | `hy3` | `hy4`, `hunyuan` | 腾讯混元增强思考推理模型，强化逻辑与代码能力 |
| **DeepSeek** | `deepseek-v3` | `deepseek-chat` | DeepSeek-V3 核心旗舰模型 |
| **OpenAI 系列** | `gpt-5.6-sol` | - | OpenAI 旗舰长程复杂推理大模型 |
| | `gpt-5.6-terra` | - | OpenAI 均衡模型，兼顾能力、速度与成本 |
| | `gpt-5.6-luna` | - | OpenAI 轻量模型，极速响应，适合日常与高并发 |
| | `gpt-5.5` | - | OpenAI 旗舰编码模型，擅长超长上下文与自主任务 |
| | `gpt-5.4` | `gpt-4o`, `gpt-4` | OpenAI 核心旗舰通用大模型 |
| | `gpt-5.3-codex`| - | OpenAI 官方特化编程辅助模型 |
| **Google 系列** | `gemini-3.1-pro` | - | Google 旗舰复杂推理模型 |
| | `gemini-3.5-flash` | - | Google 均衡超快响应多模态模型 |
| **Kimi 系列** | `kimi-k3` | `kimi` | 月之暗面 K3，擅长长程科研推理与前端代码生成 |
| | `kimi-k2.6` | - | Kimi 多模态日常高频模型 |
| | `kimi-k2.5` | - | Kimi 基础推理模型 |
| **智谱 GLM** | `glm-5.3` | - | 智谱最新 GLM-5 旗舰模型 |
| | `glm-5.2` | - | 智谱 1M 超长上下文长程任务模型 |
| **MiniMax** | `minimax-m3` | - | 原生多模态模型，擅长复杂代码与 Agent 智能体协同 |
| **智能模式** | `default-model` | `Auto` | 官方自适应智能调度模式 |
| | `fast-model` | `Fast` | 极速响应模式，适合简单任务 |
| | `balanced-model` | `Balanced` | 质量与速度兼顾，日常开发推荐 |
| | `primary-model` | `Primary` | 高质量输出，胜任复杂挑战 |
| | `deep-model` | `Deep` | 深度推理模式，适合攻坚难题 |

---

## 📦 端口与数据挂载

| 容器端口 | 协议 | 默认宿主机端口 | 用途说明 |
| :--- | :--- | :--- | :--- |
| `18090` | TCP | `18090` | **Web 控制面板**（带侧边栏折叠与容器提纯优化） |
| `18091` | TCP | `18091` | **OpenAI API 网关**（提供标准 `/v1/chat/completions`） |

- **数据卷持久化**：
  - `/data`：挂载至宿主机的 AppData 目录（如 `/mnt/user/appdata/workbuddy-switch`），持久化保存账号凭证、积分快照、Token 统计明细及 CLI 轮换状态。

---

## 🛠️ 快速部署

### Docker CLI

```bash
docker run -d \
  --name WorkBuddy-Switch \
  -p 18090:18090 \
  -p 18091:18091 \
  -v /mnt/user/appdata/workbuddy-switch:/data \
  --restart unless-stopped \
  ghcr.io/deltrivx/workbuddy-switch:latest
```

### Docker Compose

```yaml
version: '3.8'

services:
  workbuddy-switch:
    image: ghcr.io/deltrivx/workbuddy-switch:latest
    container_name: WorkBuddy-Switch
    restart: unless-stopped
    ports:
      - "18090:18090"
      - "18091:18091"
    volumes:
      - /mnt/user/appdata/workbuddy-switch:/data
```

---

## 🔌 下游客户端接入示例

### Sub2API 接入规范
1. **渠道（Channel）**：选择 `OpenAI` 格式，Base URL 填入 `http://<IP>:18091/v1`。
2. **账号类型**：选择 `apikey`，API Key 可任意填写（如 `***`）。
3. **模型映射**：将上方模型（如 `gpt-5.5`, `deepseek-v3`, `kimi-k3` 等）全量映射至对应分组即可。

### cURL 调用测试
```bash
curl -X POST http://localhost:18091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v3",
    "messages": [{"role": "user", "content": "你好！"}],
    "stream": false
  }'
```

---

## 📄 更新历史与开源协议

- 查看详细历史演进请参阅 [CHANGELOG.md](./CHANGELOG.md)。
- 本项目基于 [MIT 协议](LICENSE) 开源。
