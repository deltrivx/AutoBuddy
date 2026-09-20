# WorkBuddy Switch

<p align="center">
  <a href="https://github.com/deltrivx/workbuddy-switch">
    <img src="./icon.png" width="120" height="120" alt="WorkBuddy Switch Logo" style="border-radius: 28px; box-shadow: 0 8px 24px rgba(0,0,0,0.12);" />
  </a>
</p>

<p align="center">
  <strong>为 NAS 与服务器打造的 WorkBuddy 账号管理面板 + OpenAI 兼容 API 网关</strong>
</p>

<p align="center">
  <a href="https://github.com/deltrivx/workbuddy-switch/releases"><img src="https://img.shields.io/github/v/release/deltrivx/workbuddy-switch?color=blue&label=Release" alt="GitHub release" /></a>
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/Unraid-Compatible-F15A24?logo=unraid&logoColor=white" alt="Unraid Compatible" />
  <img src="https://img.shields.io/badge/FastAPI-Gateway-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/OpenAI_API-Compatible-412991?logo=openai&logoColor=white" alt="OpenAI Compatible" />
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License" />
</p>

<p align="center">
  <a href="#-这是什么">这是什么</a> ·
  <a href="#-核心特色">核心特色</a> ·
  <a href="#-部署方式">部署方式</a> ·
  <a href="#-使用方法">使用方法</a> ·
  <a href="#-升级方式">升级方式</a> ·
  <a href="CHANGELOG.md">更新日志</a>
</p>

---

## 📖 这是什么

`WorkBuddy Switch` 把官方的账号管理核心服务封装进一个容器，并补上两样官方没有的东西：
一个**为容器环境重做的 Web 控制台**，以及一个**自研的 OpenAI 兼容 API 网关**。

它只做两件事，且把这两件事做完整：

1. **账号管理** —— 多账号自动签到保活、积分监控、Token 刷新、用量统计；
2. **提供 API** —— 把上游全系列大模型转成标准 OpenAI 格式，供下游客户端无感接入。

官方程序是给 macOS / Windows 桌面写的（依赖 Finder、完全磁盘访问、导入本机客户端等），
直接放进容器会留下一堆点不动的按钮和永远显示「未运行」的状态图标。
本项目在**源码层面**把这些桌面专属能力物理移除，只保留容器里真正成立的功能。

| 项目 | 说明 |
| :--- | :--- |
| 定位 | WorkBuddy / CodeBuddy 账号管理 + OpenAI 兼容网关，面向 NAS 与服务器 |
| 形态 | 单容器，除 `/data` 外不需要任何其他挂载 |
| 端口 | `18090` Web 控制台 · `18091` OpenAI API 网关 |
| 镜像 | `ghcr.io/deltrivx/workbuddy-switch`（GitHub Actions 云端构建） |
| 数据 | 全部落盘 `/data`，升级不动账号数据 |
| 许可 | MIT（非官方项目） |

---

## ✨ 核心特色

### 🔄 账号自动签到与保活

- 支持 Google 国际版账号、微信扫码登录，以及备份文件导入导出。
- Token 自动保活 + 积分到期监控 + 定时自动签到，形成闭环。
- 多账号统一管理，每个账号的签到状态、积分包、可用模型一目了然。

### 🎛️ 账号池：并发分摊

- **请求级选账号**：一条对话请求仍由单个账号完成（上下文与计费不串），
  但**多个并发请求会分摊到不同账号**。
- **参与调用开关**：每个账号可单独停用或启用。
- **手动首选**：可固定只用某一个账号，再点一次回到自动分配。
- **按账号停用**：一键让某个账号退出自动轮询，再点一次恢复；
  显式指定它的请求仍可用（停用针对轮询，不是禁止调用）。
- **检测账号**：卡片上一键发轻量鉴权请求，回报凭据是否有效，不改配置、不耗额度。
- **单次覆盖**：请求可临时指定账号，不影响全局配置。
- **可观测**：每张卡显示 `已调用 N 次`，并提供接口查询各账号命中次数。

### 🚦 模型级禁用（账号 × 模型）

受优惠政策调整或时效到期影响，个别账号的个别模型可能调不通，而该账号的**其他模型仍然好用**。
因此禁用粒度落在「这个账号的这一个模型」上：

- **点一下就禁用**：账号卡片下方每个模型标签都可点击，变灰加删除线即不再参与轮询。
- **别名一并挡住**：禁用 `hy4` 会连同其目标模型 `hy3` 一起挡掉，防止换个写法绕过。
- **安全回退**：若某模型在所有账号上都被禁用，网关让上游返回真实错误，而不是编一个「无账号可用」。

### 🩺 模型可用性巡检

模型失效与恢复都是静默的 —— 坏了不会通知，恢复了也没人知道。巡检让网关定期替你试一遍：

- **一键开关**，可设巡检间隔（最短 5 分钟），默认**关闭**；可按账号圈定范围。
- **只认「上游明确说不可用」才写禁用** —— `429` 限流、超时、网络异常一律跳过不改配置。
- **禁用项分来源**：自动启用只放开巡检自己写进去的，不会抹掉你的手动禁用。
- **不碰你的手动禁用**：手动禁用的模型整个不参与探测，省掉无谓的上游请求；跳过的项数会单独标出。
- **先验凭据再探模型**：凭据失效的账号不再逐个探测其模型、也不写模型禁用（坏的是凭据不是模型）；
  确认失效时可自动停用该账号（可关），拿不到结论时不停用。
- **整轮误判保护**：整轮都没有可用信号时判定为探测机制本身出了问题，整轮作废、不写配置。
- **不拖慢网关**：探测在独立线程执行，且同一时刻只允许一轮。

### 🔌 全量模型 OpenAI 兼容网关

- 上游全系列主流大模型与官方工作模式，全部转为标准 OpenAI 格式。
- **模型清单自动发现**：从实际调用流水与实测统计中自动聚合，新模型上线无需手工补清单。
- 支持 `stream: true` / `stream: false` 双向自动转换，自动补全系统级 Prompt。

### 🔑 API 访问密钥

接入地址与密钥配置全在 WebUI 设置页里，不用翻文档：

- **密钥可选**：默认不校验，打开后 `/v1/*` 需携带密钥。
- **每把密钥独立管理**：新建、复制明文、停用、启用、删除，记录调用次数与最近使用时间。
- **两道门锁防线**：没有可用密钥时不允许开启校验；已开启时不允许停用、删除最后一个启用中的密钥，
  也不允许清空全部 —— 避免一键把自己和所有下游客户端一起关在门外。
- **连通性自检**：一键在容器内回环实测，专门用来定位「UI 能打开但 API 调不通」。

### 🖥️ 为容器重做的控制台

- **桌面专属功能物理移除**：桌面产品槽位、桌面程序状态图标、导入本机账号按钮，以及设置页的
  权限检测 / 启动设置 / 自动更新 / 限额监听等区块，全部在源码层面移除，不留死按钮与误导。
- **侧边栏可折叠**，宽窄两种模式智能切换并记忆状态。
- **Token 统计全部面板都有真实数据**：用量分布、消耗最高的调用、趋势图按模型筛选，
  由网关实时统计与官方云端历史融合。
- **响应禁缓存**，避免浏览器拿旧副本导致「改了却没生效」。
- 全链路图标高清对齐。

### 📊 Token 统计口径

- **网关转发的调用**：输入 / 输出 / 缓存命中数取自上游随响应返回的**实测用量**。
- **调用方中途断开**时收不到末帧，这类记录回退为按文本长度估算，缓存命中记为 0。
- **从官方云端流水合并进来的记录**只有积分，Token 是按固定比例**折算的估算值**。

---

## 🚀 部署方式

> 镜像全部由 GitHub Actions **云端构建**并推送到 GHCR，仓库内不含任何本地构建产物。

### Unraid 容器模板（推荐）

Unraid 上请**使用容器模板创建容器，不要手工拼接 `docker run`**：

1. 下载 [unraid/WorkBuddy-Switch.xml](./unraid/WorkBuddy-Switch.xml)（或从任意 Release 的附件中获取）；
2. Unraid 后台进入 **Docker** → **Add Container**，模板来源选择该 XML；
3. 按向导确认端口与数据目录后启动即可。

模板已内置标准容器名、官方描述、项目与支持链接、WebUI 地址与图标。
图标如需改为本地路径（离线环境），把模板里的 `<Icon>` 换成 `/mnt/user/icons/WorkBuddy-Switch.png` 即可。

### Docker Compose

```yaml
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
    environment:
      TZ: Asia/Shanghai
```

完整文件见 [docker-compose.yml](./docker-compose.yml)。

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

### 端口与数据

| 容器端口 | 默认宿主机端口 | 用途 |
| :--- | :--- | :--- |
| `18090` | `18090` | **Web 控制面板** |
| `18091` | `18091` | **OpenAI API 网关** |

- `/data` —— 挂载至宿主机的 AppData 目录，持久化保存账号凭证、积分快照、Token 统计明细、
  账号池与模型策略配置、API 密钥。**升级时数据目录不变，不会动到账号数据。**
- 删除 `/data/.wb-switch/api_keys.json` 即等于关闭密钥校验并清空全部密钥。

---

## 📘 使用方法

### 第一步：打开控制台

浏览器访问 `http://<NAS-IP>:18090`，用 Google 国际版账号或微信扫码登录，添加你的账号。
控制台会自动完成签到保活与积分监控。

### 第二步：接入下游客户端

1. 打开 `http://<NAS-IP>:18090` → **设置** → **API 接入**；
2. 点 **新建密钥**，起个名字（例如 `Sub2API`），密钥会自动复制到剪贴板；
3. 把面板里显示的 **对外访问地址** 填进客户端的 `base_url`，把密钥填进 `api_key`。

> 只在内网使用且不希望增加配置成本时，保持校验关闭即可；
> 一旦端口暴露到公网或经过隧道转发，**务必打开校验**。

### 密钥规则

- 格式 `sk-wb-` + 32 位十六进制，放在 `Authorization: Bearer <key>` 或 `x-api-key: <key>` 均可，
  `Bearer` 大小写不敏感，首尾空格会被容忍。
- 缺失 / 错误 / 已停用的密钥统一返回 `401`，并给出可读中文原因。
- **容器内回环免校验**：WebUI 代理与网关同容器，放行回环不会把外部请求放进来。

### Sub2API 配置

1. **渠道（Channel）**：选择 `OpenAI` 格式，Base URL 填入 `http://<IP>:18091/v1`。
2. **账号类型**：选择 `apikey`。若已打开**强制 API 密钥校验**，这里必须填真实密钥（`sk-wb-…`）；
   未打开校验时填任意值都能通过。
3. **模型映射**：Sub2API 的 `model_mapping` 是手工白名单、不会自动发现上游模型，
   请把下方模型目录里的模型全量映射至对应分组。

### cURL 调用测试

```bash
# 未开启密钥校验时可省略 Authorization 头
curl -X POST http://localhost:18091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-wb-你的密钥" \
  -d '{
    "model": "deepseek-v3",
    "messages": [{"role": "user", "content": "你好！"}],
    "stream": false
  }'
```

### 临时指定账号

单次请求临时指定账号，不影响全局配置：

```bash
curl -X POST http://localhost:18091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-WorkBuddy-Account-Id: <account-id>" \
  -d '{"model": "deepseek-v3", "messages": [{"role": "user", "content": "你好！"}]}'
```

也支持写在请求体里：`{"account_id": "<account-id>", ...}`，网关会在转发上游前把它摘除。

> 指定账号不存在、被停用或已过期时返回 `409`；账号池为空时返回 `401`。

---

## 🤖 模型目录

> 网关会自动发现上游新增的模型，下表列出主要可用项。以 `GET /v1/models` 的实时返回为准。

| 类别 | 模型 ID (`model`) | 别名 (`aliases`) | 说明 |
| :--- | :--- | :--- | :--- |
| **混元系列** | `hy3` | `hy4`, `hunyuan` | 腾讯混元增强思考推理模型，强化逻辑与代码能力 |
| | `hy4-preview-f` | - | 混元 Hy4 预览版 |
| **DeepSeek** | `deepseek-v3` | `deepseek-chat` | DeepSeek-V3 核心旗舰模型 |
| | `deepseek-v4.1-flash` | - | DeepSeek-V4.1 极速版 |
| **OpenAI 系列** | `gpt-5.6-sol` | - | 旗舰长程复杂推理模型 |
| | `gpt-5.6-terra` | - | 均衡模型，兼顾能力、速度与成本 |
| | `gpt-5.6-luna` | - | 轻量模型，极速响应，适合日常与高并发 |
| | `gpt-5.5` | - | 旗舰编码模型，擅长超长上下文与自主任务 |
| | `gpt-5.4` | `gpt-4o`, `gpt-4` | 核心旗舰通用大模型 |
| | `gpt-5.3-codex` | - | 官方特化编程辅助模型 |
| **Google 系列** | `gemini-3.1-pro` | - | 旗舰复杂推理模型 |
| | `gemini-3.5-flash` | - | 均衡超快响应多模态模型 |
| **Kimi 系列** | `kimi-k3` | `kimi` | 擅长长程科研推理与前端代码生成 |
| | `kimi-k2.7` / `kimi-k2.6` / `kimi-k2.5` | - | 多模态与基础推理模型 |
| **智谱 GLM** | `glm-5.3` | - | 智谱最新旗舰模型 |
| | `glm-5.2` / `glm-5.1` | - | 超长上下文长程任务模型 |
| **Anthropic** | `claude-opus-4.6` | - | Claude Opus 4.6 |
| | `claude-sonnet-4.6` | - | Claude Sonnet 4.6 |
| **CodeWise** | `codewise-model-a9` | - | CodeWise A9 |
| **MiniMax** | `minimax-m3` | - | 原生多模态，擅长复杂代码与 Agent 协同 |
| **智能模式** | `default-model` | `Auto` | 官方自适应智能调度 |
| | `fast-model` | `Fast` | 极速响应，适合简单任务 |
| | `balanced-model` | `Balanced` | 质量与速度兼顾，日常推荐 |
| | `primary-model` | `Primary` | 高质量输出，胜任复杂挑战 |
| | `deep-model` | `Deep` | 深度推理，适合攻坚难题 |

---

## 📡 接口一览

### 对外接口（`18091`）

| 接口 | 说明 |
| :--- | :--- |
| `POST /v1/chat/completions` | 对话补全 |
| `GET /v1/models` | 完整模型清单（下游同步模型列表请以此为准） |
| `GET /health` | 健康检查（**始终开放**，供容器健康检查使用） |
| `GET /gateway/info` | 连接信息 |
| `GET /rotate/status` · `POST /rotate/run` | 账号健康巡检状态与手动触发 |
| `GET /account-pool/status` · `PUT /account-pool/config` | 账号池状态与配置 |
| `POST /account-pool/toggle` · `POST /account-health/probe` | 按账号停用 / 启用、检测账号凭据 |
| `GET /account-pool/selections` · `POST /account-pool/selections/reset` | 并发分摊观测与清零 |
| `GET /account-models/config` · `PUT /account-models/config` | 模型级禁用策略 |
| `GET /model-health/config` · `PUT /model-health/config` · `GET /model-health/status` · `POST /model-health/run` | 模型可用性巡检：配置、状态与手动触发 |
| `GET /api-keys/status` · `POST /api-keys` · `POST /api-keys/update` · `POST /api-keys/delete` · `POST /api-keys/delete-all` · `PUT /api-keys/config` | 密钥管理 |

> **鉴权范围**：密钥校验只作用于 `/v1/*`。其余管理接口不拦截，它们只供本机 WebUI 与运维使用 ——
> 因此**不要把 18091 直接暴露到公网**，需要外网访问请用 WebUI（18090）或前置反向代理并自行加认证。

### 控制台接口（`18090`）

控制台通过 `/api/*` 前缀转发上述管理能力，供设置页与账号卡片使用
（如 `/api/gateway-info`、`/api/api-keys`、`/api/account-models`、`/api/model-health`、`/api/gateway-selftest`）。

### 配置文件

账号池配置持久化在 `/data/.wb-switch/account_pool_config.json`：

```json
{
  "mode": "auto",
  "enabledAccountIds": [],
  "manualAccountId": null
}
```

- `enabledAccountIds` **空数组 = 全部启用**（默认）；写入明确列表后即为白名单。
- `mode: "manual"` 时固定使用 `manualAccountId`；`"auto"` 时在已启用账号间轮询。

模型禁用策略单独存放在 `/data/.wb-switch/model_policy.json`，与账号池配置**互不覆盖**：

```json
{
  "a1b2c3d4": ["hy3", "kimi-k2.5"]
}
```

**只记禁用项** —— 未列出的模型一律视为可用，因此官方上新模型时天然是可用态，不需要迁移配置。

账号级停用策略存放在 `/data/.wb-switch/account_policy.json`，同样**与账号池配置互不覆盖**：

```json
{
  "version": 1,
  "disabled": {
    "a1b2c3d4": "manual"
  }
}
```

- 只记被停用的账号，未列出的一律参与轮询。
- 值表示来源：`manual` = 你在界面上点的，`auto` = 巡检发现凭据失效后写的。
  自动启用的逻辑只放开 `auto` 项，不会动 `manual` 项 —— 与模型禁用是同一套来源保护。
- 这份策略与账号池的 `enabledAccountIds` 是两件事：前者是「临时停用」，
  后者是「配置里的白名单」，巡检只改前者。

### 验证并发分摊

想确认「多个账号是否真的在并行分摊」：

```bash
# 1) 清零观测流水
curl -X POST http://localhost:18091/account-pool/selections/reset

# 2) 并发打若干个请求（关键是同时发出）

# 3) 看分摊结果
curl -s http://localhost:18091/account-pool/selections
```

返回的 `counts` 会列出每个账号的命中次数，`distinctAccounts` 表示实际参与调用的账号数。
也可以直接看容器日志：

```bash
docker logs WorkBuddy-Switch 2>&1 | grep '\[pool\]'
```

---

## ⬆️ 升级方式

镜像由 GitHub Actions 云端构建，升级即拉取新镜像后重建容器。

```bash
# 拉取指定版本
docker pull ghcr.io/deltrivx/workbuddy-switch:vX.Y.Z
# 或拉取最新稳定版
docker pull ghcr.io/deltrivx/workbuddy-switch:latest
```

### Unraid

在 **Docker** 页面选中该容器 → **编辑** → **应用**，走容器模板重建即可。
**不要手工拼接 `docker run`**，也不要先 `docker rm` 再手动创建 —— 那会让容器脱离 Unraid 管理。

### Docker Compose / CLI

```bash
docker compose pull && docker compose up -d
```

### 升级须知

- **数据目录不变**：账号、密钥与模型策略全部保留在 `/data`，升级不会丢配置。
- 升级后打开设置页最底部的「关于」，可核对版本号是否与 Releases 页一致。
- 每个版本改了什么，见 [CHANGELOG.md](./CHANGELOG.md) 与 [RELEASES.md](./RELEASES.md)。

---

## 📄 更新历史与许可

- **每个版本改了什么** → [CHANGELOG.md](./CHANGELOG.md)
- **版本索引与部署产物清单** → [RELEASES.md](./RELEASES.md)
- **各版本完整发布说明** → [docs/release-notes/](./docs/release-notes/)
- 本项目基于 [MIT 协议](LICENSE) 开源，非官方项目。
