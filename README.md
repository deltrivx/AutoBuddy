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
- 内置高可用 API 网关，将上游全系列顶级大模型（GPT-5.6/5.5、Gemini-3.5、DeepSeek-V3/V4.1、GLM-5.3、Kimi-K3、Claude-4.6、混元 Hy3 等 **32 款**）无缝转为标准 OpenAI 格式，供 **Sub2API**、**OpenClaw**、**NextChat**、**DSH** 等下游无感知接入。

---

## 🌟 核心特性与优化亮点

- 🖥️ **专为容器深度提纯的 WebUI**：
  - **源码级剔除无效桌面功能**：从二进制物理切除「导入本机账号」「权限检测」模块与账号卡片上的 IDE / CLI 切换槽位，避免任何无法在 Linux 执行的报错与死按钮。
  - **侧边栏可折叠收纳**：支持 `220px` 与 `68px` 极简图标模式智能切换，具备 `localStorage` 状态持久化记忆。
  - **纯粹清晰的 Token 统计**：剔除冗余分组 Tab 按钮与「Token 总览」重复标题，自研网关实时统计与云端历史融合引擎，告别空白报表。
  - **全链路图标高清对齐**：侧边栏、Header 与 Favicon 全面同步 Unraid 512×512 官方圆角高清图标。
- 🔄 **全自动保活与 CLI 接入**：
  - 容器启动全自动初始化 CodeBuddy CLI 凭证与 Helper，彻底告别「未接入 CLI」报警。
  - 支持 Google 国际版账号、微信扫码登录与备份文件快速导入导出。
- 🚀 **全量模型 OpenAI 兼容网关**：
  - 支持 **32 款主流顶级大模型与工作模式别名** 端到端极速调用。
  - **模型清单自动发现**：官方未提供 `/models` 接口，网关改从实际调用流水（`official_usage_cache.json`）与网关实测统计中自动聚合模型，新模型上线后无需手工补清单。
  - 完美支持 `stream: true` 与 `stream: false` 自动双向流/非流转换。
  - 自动补全系统级 Prompt（`normalize_messages`），保障上游 100% 稳定响应。
- 🧾 **账号卡片动态模型清单**：
  - 每个账号下方自动展示**该账号可调用的模型**，数据源为网关 `/v1/models`，与全局清单保持一致，不做任何硬编码。
  - 已真实调用过的模型高亮标注，悬停可见「调用次数 / 消耗积分」。
  - 刚添加、尚无调用记录的新账号同样完整展示可用清单，并标注「暂无调用记录」。
- 🔄 **容器原生的账号轮询与账号池**：
  - 官方轮询依赖重启宿主机桌面客户端，容器内无法执行。网关内置轮询每 30 分钟检测当前账号可用性并自动切换，维护 `rotate/state.json`（仅用于健康状态与「当前账号」展示）。
  - **API 请求不再走 `activeAccountId`**，而是每次请求调用 `select_account()` 按账号池配置独立选账号，实现并发分摊。
  - 可通过 `GATEWAY_ROTATE_ENABLED` / `GATEWAY_ROTATE_INTERVAL_MINUTES` 调整，状态见 `/rotate/status`。
- 🧹 **彻底移除容器内无效的桌面状态**：
  - 账号卡片上的 WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 三个「设为当前账号」槽位，**已从二进制里物理删除**（整块 `footer` 表达式抹除，DOM 中不再生成），不再依赖任何 CSS 遮掩。
  - 右上角三个桌面程序状态图标同样物理移除（容器内无宿主客户端，状态恒为「未运行 / 未安装」，只造成误导）。
  - `Kb`「当前账号」徽章组件返回值同步清空；配套的前端遮掩代码已全部删除，避免双份维护。
- 🎛️ **账号池：并行调用 + 手动首选**：
  - **请求级选账号**：一条对话请求仍由单个账号完成（上下文与计费不串），但**多个并发请求会分摊到不同账号**，不再全部挤在 `activeAccountId`。
  - **启用开关**：每个账号卡片带 `[参与调用 · 点击停用]` 按钮，`enabledAccountIds` 为空数组时语义为「全部启用」，保存明确列表后即成为白名单。
  - **手动首选**：`[设为首选]` 固定只用某一个账号（`mode=manual`），再点一次回到自动分配。
  - **单次覆盖**：请求可带 `X-WorkBuddy-Account-Id` 头或 `body.account_id` 临时指定账号，不影响全局配置。
  - 候选账号会自动剔除 token 缺失或 `expiresAt` 已过期的账号。
- 🔗 **开箱即用对接 Sub2API**：
  - 完美适配 Sub2API 的 `apikey` 鉴权与渠道路由，实现多账号轮询与配额统计。
  - Sub2API 的 `model_mapping` 为手工白名单，不会自动发现上游模型；可将网关 `/v1/models` 的返回同步进去。

---

## 🤖 官方全量支持模型目录

| 模型类别 | 模型 ID (`model`) | 别名映射 (`aliases`) | 官方说明与能力特性 |
| :--- | :--- | :--- | :--- |
| **混元系列** | `hy3` | `hy4`, `hunyuan` | 腾讯混元增强思考推理模型，强化逻辑与代码能力 |
| | `hy4-preview-f` | - | 混元 Hy4 预览版（自动发现） |
| **DeepSeek** | `deepseek-v3` | `deepseek-chat` | DeepSeek-V3 核心旗舰模型 |
| | `deepseek-v4.1-flash` | - | DeepSeek-V4.1 极速版（自动发现） |
| **OpenAI 系列** | `gpt-5.6-sol` | - | OpenAI 旗舰长程复杂推理大模型 |
| | `gpt-5.6-terra` | - | OpenAI 均衡模型，兼顾能力、速度与成本 |
| | `gpt-5.6-luna` | - | OpenAI 轻量模型，极速响应，适合日常与高并发 |
| | `gpt-5.5` | - | OpenAI 旗舰编码模型，擅长超长上下文与自主任务 |
| | `gpt-5.4` | `gpt-4o`, `gpt-4` | OpenAI 核心旗舰通用大模型 |
| | `gpt-5.3-codex`| - | OpenAI 官方特化编程辅助模型 |
| **Google 系列** | `gemini-3.1-pro` | - | Google 旗舰复杂推理模型 |
| | `gemini-3.5-flash` | - | Google 均衡超快响应多模态模型 |
| **Kimi 系列** | `kimi-k3` | `kimi` | 月之暗面 K3，擅长长程科研推理与前端代码生成 |
| | `kimi-k2.7` | - | Kimi-K2.7（自动发现） |
| | `kimi-k2.6` | - | Kimi 多模态日常高频模型 |
| | `kimi-k2.5` | - | Kimi 基础推理模型 |
| **智谱 GLM** | `glm-5.3` | - | 智谱最新 GLM-5 旗舰模型 |
| | `glm-5.2` | - | 智谱 1M 超长上下文长程任务模型 |
| | `glm-5.1` | - | GLM-5.1（自动发现） |
| **Anthropic** | `claude-opus-4.6` | - | Claude Opus 4.6（自动发现） |
| | `claude-sonnet-4.6` | - | Claude Sonnet 4.6（自动发现） |
| **CodeWise** | `codewise-model-a9` | - | CodeWise A9（自动发现） |
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

### 网关内置接口

| 接口 | 说明 |
| :--- | :--- |
| `GET /v1/models` | 返回自动发现后的完整模型清单（下游同步模型列表请以这里为准） |
| `GET /health` | 健康检查，含 `models_count`、`models_auto_discovered` 与轮询状态快照 |
| `GET /rotate/status` | 查看账号轮询开关、间隔、上次检查与上次切换结果 |
| `POST /rotate/run` | 立即执行一次账号可用性检测与切换 |
| `GET /account-pool/status` | 账号池状态：模式、启用列表、首选账号、各账号 `enabled` / `usable` / `active` |
| `PUT /account-pool/config` | 更新账号池配置（`mode` / `enabledAccountIds` / `manualAccountId`） |
| `GET /api/account-models` | Web 控制台用：按账号返回「可用模型 + 已调用模型 + 用量」 |
| `GET /api/account-pool` | Web 控制台用：转发网关账号池状态 |
| `PUT /api/account-pool` | Web 控制台用：转发账号池配置更新 |

### 账号池与手动指定账号

账号池配置持久化在 `/data/.wb-switch/account_pool_config.json`：

```json
{
  "mode": "auto",
  "enabledAccountIds": [],
  "manualAccountId": null
}
```

- `enabledAccountIds` **空数组 = 全部启用**（默认）；写入明确列表后即为白名单。
- `mode: "manual"` 时固定使用 `manualAccountId`；`"auto"` 时在已启用账号间 round-robin。

单次请求临时指定账号（不影响全局配置）：

```bash
curl -X POST http://localhost:18091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-WorkBuddy-Account-Id: <account-id>" \
  -d '{"model": "deepseek-v3", "messages": [{"role": "user", "content": "你好！"}]}'
```

也支持写在请求体里：`{"account_id": "<account-id>", ...}`。网关会在转发上游前把它从 body 中摘除。

> 指定账号不存在、被停用或已过期时返回 `409`；账号池为空时返回 `401`。

---

## 📄 更新历史与开源协议

- 查看详细历史演进请参阅 [CHANGELOG.md](./CHANGELOG.md)。
- 本项目基于 [MIT 协议](LICENSE) 开源。
