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
  - **源码级剔除无效桌面功能**：从二进制物理切除账号卡片上的三个桌面图标（WorkBuddy / CodeBuddy IDE / CodeBuddy CLI）、桌面程序运行状态图标、导入本机账号按钮，以及设置页的**权限检测 / 启动设置 / 自动更新 / 限额监听 / CodeBuddy CLI 自动轮换**五个区块——这些功能要么依赖宿主桌面客户端，要么在容器里靠镜像重建升级，留在界面上只会产生死按钮与误导。
  - **补丁失效即构建失败**：官方发版会重排压缩变量名（`0.1.36` 的 `m.jsx` 到 `0.1.40` 已变成 `p.jsx`），因此补丁锚点全部改为按稳定文案 / `className` / `id` 定位、并锁定 wb-switch 版本；任一锚点失配时镜像构建直接失败，不会像以前那样静默上线。
  - **侧边栏可折叠收纳**：支持 `220px` 与 `68px` 极简图标模式智能切换，具备 `localStorage` 状态持久化记忆。
  - **纯粹清晰的 Token 统计**：剔除冗余分组 Tab 按钮与「Token 总览」重复标题，自研网关实时统计与云端历史融合引擎；`用量分布` / `消耗最高的调用` / `趋势图按模型筛选` 全部有真实数据，告别空白报表。
  - **文案按容器能力适配**：官方前端是给桌面客户端写的。代理层对首页副标题、统计页维度标签等做**等长字节替换**（`按项目→按账号`、`消耗最高的会话→消耗最高的调用`），既改对语义又不破坏 JS 资源的偏移与语法。
  - **响应禁缓存**：`assets/*.js` 文件名带 hash 不会随改动变化，代理对 HTML / JS 响应强制 `cache-control: no-store`，避免浏览器拿旧副本导致「改了却没生效」。
  - **全链路图标高清对齐**：侧边栏、Header 与 Favicon 全面同步 Unraid 512×512 官方圆角高清图标。
- 🔄 **全自动保活与签到**：
  - 支持 Google 国际版账号、微信扫码登录与备份文件快速导入导出。
  - 账号 Token 自动保活 + 积分到期监控 + 定时自动签到，形成闭环。
- 🚀 **全量模型 OpenAI 兼容网关**：
  - 支持 **32 款主流顶级大模型与工作模式别名** 端到端极速调用。
  - **模型清单自动发现**：官方未提供 `/models` 接口，网关改从实际调用流水（`official_usage_cache.json`）与网关实测统计中自动聚合模型，新模型上线后无需手工补清单。
  - 完美支持 `stream: true` 与 `stream: false` 自动双向流/非流转换。
  - 自动补全系统级 Prompt（`normalize_messages`），保障上游 100% 稳定响应。
- 🔑 **API 接入信息与访问密钥（全部在设置页完成）**：
  - 设置页新增 **API 接入** 面板：对外地址、接口端点、一键复制、密钥开关、密钥增删改、连通性自检、可直接使用的 `curl` 示例——不用翻文档就能把下游客户端接上。
  - **密钥是可选的**：默认不校验，行为与升级前完全一致；打开后 `/v1/*` 需携带 `Authorization: Bearer sk-wb-…` 或 `x-api-key: sk-wb-…`。
  - **每把密钥独立管理**：`sk-wb-` + 32 位十六进制，界面显示掩码、可随时复制明文，支持单独启用/停用/删除，并记录调用次数与最近使用时间。
  - **两道门锁防线**：没有任何可用密钥时不允许开启校验；已开启校验时不允许停用/删除最后一个启用中的密钥、也不允许清空全部——避免一键把自己和所有下游客户端一起关在门外。
- 🧾 **账号卡片动态模型清单（网关归因，不依赖上游）**：
  - 每个账号下方自动展示**该账号可调用的模型**，数据源为网关 `/v1/models`，与全局清单保持一致，不做任何硬编码。
  - **已调用模型由网关自己归因**：每次 API 调用都会把实际服务该请求的账号写进 `token_stats_logs.json`，因此**每个账号都能显示自己调用过哪些模型**。
  - 上游 `official_usage_cache.json` 只对「当前账号」返回模型明细（其余账号 `models` 恒为空），因此它只作补充，不作主数据源——否则会出现「只有一个账号有调用记录」的假象。
  - 悬停标签可见「网关调用次数 / 估算 Token / 官方记录次数 / 消耗积分」。
  - 刚添加、尚无调用记录的新账号同样完整展示可用清单，并标注「暂无调用记录」。
- 🔄 **两层账号调度链路（互补，不冲突）**：

  | 层 | 机制 | 作用范围 | 默认间隔 |
  | :--- | :--- | :--- | :--- |
  | ① **账号池**（网关 `select_account()`） | 每次 API 请求独立选账号，round-robin | **决定 API 实际用哪个账号** | 每请求 |
  | ② **网关健康巡检**（`rotate_once()`） | 当前账号失效时才切换，维护 `rotate/state.json` | 仅供健康状态展示与兼容旧接口 | 30 分钟 |

  - **①是权威**：API 请求不再走 `activeAccountId`，而是每次请求按账号池配置独立选账号，实现并发分摊。
  - **②不参与 API 调度**：它只在当前账号失效时做健康切换，维护 `rotate/state.json` 供状态展示。
  - 可通过 `GATEWAY_ROTATE_ENABLED` / `GATEWAY_ROTATE_INTERVAL_MINUTES` 调整 ②，状态见 `/rotate/status`。
- 🧹 **彻底移除容器内无效的桌面状态**：
  - 账号卡片头部三个桌面图标（WorkBuddy / CodeBuddy IDE / CodeBuddy CLI）与底部三个产品槽位，**已从二进制里物理删除**（整块 JSX 表达式抹成 `null`，DOM 中不再生成），不再依赖任何 CSS 遮掩。
  - 右上角三个桌面程序状态图标同样物理移除（容器内无宿主客户端，状态恒为「未运行 / 未安装」，只造成误导）。
  - 「当前账号」徽章组件返回值同步清空。
- 🎛️ **账号池：并行调用 + 手动首选**：
  - **请求级选账号**：一条对话请求仍由单个账号完成（上下文与计费不串），但**多个并发请求会分摊到不同账号**，不再全部挤在 `activeAccountId`。
  - **启用开关**：每个账号卡片带 `[参与调用 · 点击停用]` 按钮，`enabledAccountIds` 为空数组时语义为「全部启用」，保存明确列表后即成为白名单。
  - **手动首选**：`[设为首选]` 固定只用某一个账号（`mode=manual`），再点一次回到自动分配。
  - **单次覆盖**：请求可带 `X-WorkBuddy-Account-Id` 头或 `body.account_id` 临时指定账号，不影响全局配置。
  - **可观测**：每张卡显示 `已调用 N 次` 计数徽章；`GET /account-pool/selections` 返回各账号命中次数，用于实证「并发是否真的分摊」。
  - 候选账号会自动剔除 token 缺失或 `expiresAt` 已过期的账号。
- 🚦 **模型级禁用：账号 × 模型的精细拉黑**：
  账号提供的模型很多，但受**优惠政策调整**或**时效到期**影响，个别账号的个别模型可能调不通。
  停用整个账号损失太大，所以禁用粒度落在「**这个账号的这一个模型**」上。
  - **点一下就禁用**：账号卡片下方的每个模型标签都是可点击的。点一下变灰 + 删除线 = 该模型不再参与这个账号的轮询；再点恢复原色。
  - **与「已调用」不冲突**：标签共三态 —— 默认（可用）、蓝色高亮（该账号真实调用过）、灰化删除线（已禁用）。禁用态**优先覆盖**已用高亮，避免你刚点掉的那个看起来没反应。
  - **状态可见**：卡片标题旁出现 `已禁用 N` 徽章，折叠时也能看出这里动过手。
  - **别名一并挡住**：禁用 `hy4` 会连同其目标模型 `hy3` 一起挡掉，防止调用方换个写法绕过。
  - **安全回退**：若某个模型在**所有**账号上都被禁用，网关不会返回「无账号可用」，而是回退到不过滤，让上游返回真实错误 —— 比在网关层编一个错误更利于排查。
  - 策略持久化在 `/data/.wb-switch/model_policy.json`，与账号池配置**互不覆盖**；文件损坏时回退为空策略，绝不影响正常调用。
- 📊 **Token 统计：全部面板都有真实数据**：
  - **用量分布**：`按账号`（网关归因）与 `按模型` 两个维度，不再空白。
  - **消耗最高的调用**：按单次 API 调用的 Token 从高到低排列，标题为模型名、副标题为服务账号。
  - **Token 与调用趋势**：支持按模型筛选（`dailyByModel` 已填充）。
  - **口径说明**：网关自身记录的是**实测估算** Token（按文本长度 / 3.5 估算）；从官方云端流水合并进来的记录只有「积分」，其 Token 是按固定比例**折算的估算值**。两者都会出现在同一张报表里，判断绝对量级时请注意这一差异。
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
  - `/data`：挂载至宿主机的 AppData 目录（如 `/mnt/user/appdata/workbuddy-switch`），持久化保存账号凭证、积分快照、Token 统计明细及轮换状态。
  - `/data/.wb-switch/api_keys.json`：API 访问密钥与「强制校验」开关（权限 `0600`）。删除该文件即等于关闭校验并清空全部密钥。

> 本镜像只做两件事：**账号自动签到保活** 与 **提供 OpenAI 兼容 API**。除 `/data` 外不需要任何其他挂载。

---

## 🛠️ 快速部署

> 镜像全部由 GitHub Actions **云端构建**并推送到 GHCR，仓库内不含任何本地构建产物。

### Unraid 容器模板（推荐）

Unraid 上请**使用容器模板创建容器，不要手工拼接 `docker run`**：

1. 下载 [unraid/WorkBuddy-Switch.xml](./unraid/WorkBuddy-Switch.xml)（或从任意 Release 的附件中获取）；
2. Unraid 后台进入 **Docker** → **Add Container**，模板来源选择该 XML；
3. 按向导确认端口与数据目录后启动即可。

模板已内置：标准容器名 `WorkBuddy-Switch`、官方 Overview 描述、项目与支持链接、WebUI 地址与图标。

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
      - ./workbuddy-switch/data:/data
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

## 🔑 API 接入与访问密钥

容器的第二个核心能力是「对外提供 OpenAI 兼容 API」。**接入地址**与**访问密钥**都直接放在 WebUI 的
**设置 → API 接入** 里，不需要去翻文档。

### 面板提供什么

| 项 | 说明 |
| :--- | :--- |
| 对外访问地址 | 按当前访问的主机名自动推导为 `http(s)://<host>:18091/v1`；走域名或隧道时可手动改，改动记在浏览器本地 |
| 接口端点 | `POST /v1/chat/completions`、`GET /v1/models`，点一下即复制完整地址 |
| 网关状态 | 版本、账号池模式、可路由模型数、可用账号数与参与调用的账号数 |
| 密钥校验开关 | 「强制 API 密钥校验」一键开/关，**默认关闭** |
| 密钥管理 | 新建、复制明文、停用、启用、删除、清空；列表显示掩码、调用次数与最近使用时间 |
| 连通性自检 | 在容器内回环实测「网关健康 / 模型清单 / 密钥配置自洽性」三步，用来定位「UI 能打开但 API 调不通」 |
| 调用示例 | 自动带上当前启用密钥的 `curl` 片段，复制即用 |

### 密钥规则

- 格式为 `sk-wb-` + 32 位十六进制。调用时放在 `Authorization: Bearer <key>` 或 `x-api-key: <key>`，两种都支持，`Bearer` 大小写不敏感。
- **默认不校验**：没有配置密钥的用户，行为与升级前完全一致，`/v1/*` 直接可调。
- **打开校验后**：缺失密钥返回 `401`（提示缺少密钥）、密钥错误返回 `401`（提示无效）、密钥被停用同样 `401`。
- **容器内回环免校验**：WebUI 代理与网关同容器，它要读 `/v1/models` 来渲染账号卡片的模型清单，不能被自己的密钥挡住。容器外的请求经 docker NAT 进来，源地址是网桥地址而非 `127.0.0.1`，因此放行回环是安全的。
- **两道门锁防线**（防止把自己关在门外）：
  1. 一个可用密钥都没有时，不允许开启校验；
  2. 已开启校验时，不允许停用/删除最后一个启用中的密钥，也不允许清空全部。
- 密钥明文保存在 `/data/.wb-switch/api_keys.json`（权限 `0600`），这样才能在界面里随时复制核对。

### 从 0 接一个下游客户端

1. 打开 `http://<NAS-IP>:18090` → **设置** → **API 接入**；
2. 点 **新建密钥**，起个名字（例如 `Sub2API`），密钥会自动复制到剪贴板；
3. 把面板里显示的 **对外访问地址** 填进客户端的 `base_url`，把密钥填进 `api_key`；
4. 需要收紧访问时，把 **强制 API 密钥校验** 打开即可 —— 此时所有未携带合法密钥的调用都会被拒。

> 只在内网使用且不希望增加配置成本时，保持校验关闭即可；一旦端口暴露到公网或经过隧道转发，**务必打开校验**。

---

## 🔌 下游客户端接入示例

### Sub2API 接入规范
1. **渠道（Channel）**：选择 `OpenAI` 格式，Base URL 填入 `http://<IP>:18091/v1`。
2. **账号类型**：选择 `apikey`。若已在设置页打开 **强制 API 密钥校验**，这里必须填真实密钥（`sk-wb-…`）；
   未打开校验时填任意值（如 `***`）都能通过。
3. **模型映射**：将上方模型（如 `gpt-5.5`, `deepseek-v3`, `kimi-k3` 等）全量映射至对应分组即可。

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

### 网关内置接口

| 接口 | 说明 |
| :--- | :--- |
| `GET /v1/models` | 返回自动发现后的完整模型清单（下游同步模型列表请以这里为准）；开启密钥校验后需携带密钥 |
| `POST /v1/chat/completions` | 对话补全；开启密钥校验后需携带密钥 |
| `GET /health` | 健康检查，含 `version`、`models_count`、`models_auto_discovered`、`require_api_key` 与轮询状态快照（**始终开放**，供容器健康检查使用） |
| `GET /gateway/info` | 连接信息：`basePath`、`endpoints`、`auth`、模型与账号规模、`features` |
| `GET /api-keys/status` | 密钥状态：`requireKey`、密钥列表（含掩码）与汇总 `stats`，并附带 `gateway` 信息 |
| `POST /api-keys` | 新建密钥，`{"name": "Sub2API"}`，返回明文密钥 |
| `POST /api-keys/update` | 改名 / 启用 / 停用，`{"id": "…", "enabled": false}` |
| `POST /api-keys/delete` | 删除单把密钥，`{"id": "…"}` |
| `POST /api-keys/delete-all` | 清空全部密钥（开启校验时会被拒） |
| `PUT /api-keys/config` | 开关强制校验，`{"requireKey": true}` |
| `GET /rotate/status` | 查看**网关健康巡检**的开关、间隔、上次检查与上次切换结果（不参与 API 调度，见上文三层链路） |
| `POST /rotate/run` | 立即执行一次账号可用性检测与切换 |
| `GET /account-pool/status` | 账号池状态：模式、启用列表、首选账号、各账号 `enabled` / `usable` / `active`、`selectionCounts` |
| `PUT /account-pool/config` | 更新账号池配置（`mode` / `enabledAccountIds` / `manualAccountId`） |
| `GET /account-pool/selections` | 并发分摊观测：`total` / `distinctAccounts` / `counts`（各账号命中次数）/ `recent` |
| `POST /account-pool/selections/reset` | 清空选账号流水，便于重新压测观测 |
| `GET /account-models/config` | 读取模型级禁用策略：`policy`（账号 → 禁用模型列表）/ `disabledTotal` |
| `PUT /account-models/config` | 切换某账号某模型的禁用状态（`{"accountId","model","disabled"}`，或 `{"accountId","models":[…]}` 整份覆盖） |
| `GET /api/account-models` | Web 控制台用：按账号返回「可用模型 + 已调用模型 + 用量 + `disabled` 禁用列表」 |
| `PUT /api/account-models` | Web 控制台用：转发模型禁用切换（点模型标签触发） |
| `GET /api/account-pool` | Web 控制台用：转发网关账号池状态 |
| `PUT /api/account-pool` | Web 控制台用：转发账号池配置更新 |
| `GET /api/account-pool/selections` | Web 控制台用：转发并发分摊观测数据 |
| `POST /api/account-pool/selections/reset` | Web 控制台用：转发清零选账号流水 |
| `GET /api/api-keys` | Web 控制台用：转发密钥状态（设置页数据源） |
| `POST /api/api-keys` `/update` `/delete` `/delete-all` | Web 控制台用：转发密钥增删改 |
| `PUT /api/api-keys/config` | Web 控制台用：转发密钥校验开关 |
| `GET /api/gateway-info` | Web 控制台用：转发连接信息 |
| `POST /api/gateway-selftest` | Web 控制台用：容器内回环自检（健康 / 模型清单 / 密钥配置自洽性） |

> **接口分工**：决定 API 用哪个账号的是 `select_account()`（账号池）；`/rotate/*` 只做账号健康巡检，
> 不参与 API 调度。
>
> **鉴权范围**：密钥校验只作用于对外的 `/v1/*`。`/health`、`/gateway/info`、`/api-keys/*`、`/account-pool/*`
> 等管理接口不拦截，它们只供本机 WebUI 与运维使用 —— 因此**不要把 18091 直接暴露到公网**，
> 需要外网访问请用 WebUI（18090）或前置反代并自行加认证。

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

#### 验证并发分摊

想确认「多个账号是否真的在并行分摊」，而不是只用了其中一个：

```bash
# 1) 清零观测流水
curl -X POST http://localhost:18091/account-pool/selections/reset

# 2) 并发打 20 个请求（任意客户端，关键是同时发出）

# 3) 看分摊结果
curl -s http://localhost:18091/account-pool/selections
```

返回的 `counts` 会列出每个账号的命中次数；`distinctAccounts` 表示实际参与调用的账号数。
也可以直接看容器日志：

```bash
docker logs WorkBuddy-Switch 2>&1 | grep '\[pool\]'
```

WebUI 里每个账号卡片上也会实时显示 `已调用 N 次` 计数徽章。

> 语义边界：**一条对话请求仍由单个账号完成**（不拆请求，避免上下文与计费混乱），
> **多个同时到达的独立请求才会分摊到不同账号**。

---

## 🚦 模型级禁用（账号 × 模型）

### 为什么需要它

账号池解决的是「**哪个账号**参与调用」。但实际使用中还有一层更细的问题：

> 一个账号提供了几十个模型，其中**个别模型**因为优惠政策调整、免费额度到期或上游限流，
> 在这个账号上已经调不通了 —— 可是这个账号的**其他模型仍然好用**。

这时候停用整个账号是过度反应（损失掉还能用的模型），放任不管又会让轮询时不时撞上
那个死模型、把请求打失败。所以禁用粒度必须是「**这个账号的这一个模型**」。

### 怎么用

账号卡片下方每个模型标签**都是可点击的**：

| 状态 | 外观 | 含义 |
| :--- | :--- | :--- |
| 默认 | 灰底常规字 | 可用，参与轮询 |
| 已用 | 蓝底加粗 | 该账号**真实调用过**这个模型（原有设计，保留） |
| **已禁用** | **灰底 + 删除线** | **点击后进入，不再参与该账号的轮询** |

- **点击切换**：点一下禁用，再点一下恢复原色（若该模型被调用过，恢复后回到蓝色高亮态）。
- **禁用优先于已用**：禁用态会覆盖蓝色高亮 —— 否则刚点掉的那个看起来「没反应」。
- **计数徽章**：卡片标题旁显示 `已禁用 N`，账号卡片折叠时也能看出这里动过手。
- 支持键盘操作（`Tab` 聚焦 + `Enter` / `Space` 触发）。

### 生效范围

禁用只影响**账号池的自动分配与手动指定**，具体表现：

1. 该账号**不再被分配到**这个模型的请求（`select_account(model=...)` 会跳过它）；
2. 该账号的**其他模型完全不受影响**；
3. **别名一并挡住**：禁用 `hy4` 会连同目标模型 `hy3` 一起挡掉 —— 否则调用方换个写法就能绕过；
4. 若某模型在**所有**账号上都被禁用，网关**不返回 401/409**，而是回退到不过滤，
   让上游返回真实错误。在网关层编一个「无账号可用」会掩盖真实原因，不利于排查。

### 持久化与容错

策略落在 `/data/.wb-switch/model_policy.json`，结构与账号池配置**完全分离**（互不覆盖）：

```json
{
  "a1b2c3d4": ["hy3", "kimi-k2.5"]
}
```

- **只记禁用项**：未列出的模型一律视为可用。这样官方上新模型时天然是可用态，不需要迁移配置。
- 写入使用 `tmp` + `os.replace` 原子替换，不会留下半截 JSON。
- 文件缺失 / 损坏 / 结构不对时一律回退为**空策略**（即全部可用），绝不抛异常影响调用。

---

## 🔧 容器化补丁机制（维护须知）

官方 `wb-switch` 是单文件二进制，前端 React 代码内嵌其中，无法直接改源码。本项目用
`patch/patch_binary.py` 对它做**等长字节替换**（改动字节数必须完全一致，否则会破坏二进制内的偏移表）。

两条纪律，改动补丁时务必遵守：

1. **锚点不得依赖压缩变量名**。官方每次发版都会重排 minify 变量名——`0.1.36` 用的是 `m.jsx` / `Db`，
   到 `0.1.40` 已变成 `p.jsx` / `zb`。所以锚点一律用稳定的**文案 / `className` / `id`** 定位，
   再回溯到 JSX 调用起点，变量名从匹配结果里现取。
2. **锚点失配必须让构建失败**。`patch_binary.py` 会收集所有失败项并以非零码退出，Dockerfile 里
   没有 `|| true`，因此锚点一旦失配镜像构建就会失败，不会静默上线。

### 升级上游版本

```bash
# 1. 拉一份新版本二进制，本地先验证锚点
npm pack workbuddy-switch-linux-x64@<新版本> && tar xzf workbuddy-switch-linux-x64-*.tgz
python3 patch/patch_binary.py package/bin/wb-switch-linux-x64

# 2. 全部 ok 后再改 docker/Dockerfile 里的 WB_SWITCH_VERSION，然后构建
```

若某条锚点报 `FAIL`，说明官方前端结构变了：用
`python3 -c "..."` 或 `grep -a -o -E '.{200}<原锚点片段>.{200}' <二进制>`
看清新结构，再更新对应锚点。

---

## 📄 更新历史与开源协议

- 版本索引与每个版本的部署产物： [RELEASES.md](./RELEASES.md)。
- 查看详细历史演进请参阅 [CHANGELOG.md](./CHANGELOG.md)。
- 各版本的完整发布说明收录在 [docs/release-notes/](./docs/release-notes/)。
- 改了 CHANGELOG 后请跑 `python3 scripts/gen_releases.py` 重新生成版本索引，避免两处漂移。
- 本项目基于 [MIT 协议](LICENSE) 开源。
