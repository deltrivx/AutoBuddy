# WorkBuddy Switch

<p align="center">
  <a href="https://github.com/deltrivx/workbuddy-switch">
    <img src="./icon.png" width="120" height="120" alt="WorkBuddy Switch Logo" style="border-radius: 28px; box-shadow: 0 8px 24px rgba(0,0,0,0.12);" />
  </a>
</p>

<p align="center">
  <strong>为 NAS 与服务器打造的 WorkBuddy / CodeBuddy 账号管理面板与 OpenAI 兼容 API 网关</strong>
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
  <a href="#-快速部署">快速部署</a> ·
  <a href="#-核心特性">核心特性</a> ·
  <a href="#-api-接入">API 接入</a> ·
  <a href="#-模型目录">模型目录</a> ·
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

> 除 `/data` 之外，不需要任何其他挂载。

---

## ✨ 核心特性

### 🔄 账号自动签到与保活

- 支持 Google 国际版账号、微信扫码登录，以及备份文件导入导出。
- Token 自动保活 + 积分到期监控 + 定时自动签到，形成闭环。
- 多账号统一管理，每个账号的签到状态、积分包、可用模型一目了然。

### 🎛️ 账号池：并发分摊 + 手动指定

- **请求级选账号**：一条对话请求仍由单个账号完成（上下文与计费不串），
  但**多个并发请求会分摊到不同账号**，不再全部挤在同一个账号上。
- **参与调用开关**：每个账号可单独停用或启用。
- **手动首选**：可固定只用某一个账号，再点一次回到自动分配。
- **单次覆盖**：请求可临时指定账号，不影响全局配置。
- **可观测**：每张卡显示 `已调用 N 次`，并提供接口查询各账号命中次数，
  用来实证「并发是否真的分摊」。

### 🚦 模型级禁用：账号 × 模型的精细拉黑

账号提供的模型很多，但受**优惠政策调整**或**时效到期**影响，个别账号的个别模型可能调不通，
而这个账号的**其他模型仍然好用**。停用整个账号损失太大，所以禁用粒度落在「**这个账号的这一个模型**」上。

- **点一下就禁用**：账号卡片下方每个模型标签都可点击，变灰加删除线即不再参与该账号的轮询。
- **三态清晰**：默认（可用）/ 蓝色高亮（该账号真实调用过）/ 灰化删除线（已禁用），
  禁用态优先覆盖已用高亮。
- **别名一并挡住**：禁用 `hy4` 会连同其目标模型 `hy3` 一起挡掉，防止换个写法绕过。
- **安全回退**：若某模型在**所有**账号上都被禁用，网关不会返回「无账号可用」，
  而是让上游返回真实错误 —— 比在网关层编一个错误更利于排查。

### 🔑 API 接入与访问密钥

接入地址与密钥配置**全在 WebUI 的设置页里**，不用翻文档：

- **密钥可选**：默认不校验，行为与不设密钥时完全一致；打开后 `/v1/*` 需携带密钥。
- **每把密钥独立管理**：可新建、复制明文、停用、启用、删除，并记录调用次数与最近使用时间。
- **两道门锁防线**：没有任何可用密钥时不允许开启校验；已开启校验时不允许停用、删除
  最后一个启用中的密钥，也不允许清空全部 —— 避免一键把自己和所有下游客户端一起关在门外。
- **连通性自检**：一键在容器内回环实测，专门用来定位「UI 能打开但 API 调不通」。

### 🩺 模型可用性巡检：调不通自动禁用，恢复自动启用

手动禁用解决了「知道哪个模型坏了」的场景，但模型失效与恢复**都是静默的** ——
坏了不会通知，恢复了也没人知道。这一版让网关定期替你试一遍。

- **一键开关**：设置页可开启 / 关闭自动巡检，并设定巡检间隔（最短 5 分钟）。默认**关闭**。
- **可按账号圈定范围**：不选即全部账号参与，也可以只勾选部分账号。
- **三档判定，宁漏勿误**：`429` 限流、超时与网络异常一律**跳过、不改配置** ——
  上游高峰期限流是常态，把它当故障会在高峰后留下一批被误禁的好模型。
- **只探「用过的模型」**：默认只检测该账号实际调用过的模型，避免组合数量失控；
  新账号无调用记录时退回内置基础清单，保证也能被覆盖。
- **禁用项分来源，手动与自动互不侵犯**：每个禁用项标明是你在卡片上点掉的（手动），
  还是探测判定不可用后写入的（巡检）。自动启用**只放开自己写进去的那些** ——
  手动禁用通常不是「坏了」而是「能用但不想用」，不该被自愈抹掉。
- **同一份策略**：巡检写入的就是手动禁用用的那一份配置，**关掉巡检后已写入的禁用项继续生效**，
  随时可以在账号卡片上手动启用 / 重新禁用。
- **整轮误判保护**：一整轮里若没有任何组合可用、且不可用数量达到门槛，
  判定为探测机制本身出了问题，**整轮作废、不写任何配置**并在面板上说明原因。
  模型不会约好了同时失效 —— 宁可漏禁一轮，也不误禁一批。
- **改动前自动留底**：一轮巡检可能同时改动上百个条目，落盘前先备份策略文件，
  出问题可整份回退。
- **不拖慢网关**：探测在独立线程执行，且同一时刻只允许一轮巡检，转发请求不受影响。
- **可即时手动巡检**：面板上的「立即巡检」不改变开关状态，跑完直接回显本轮结果与变更明细，
  面板还会显示「手动禁用 N 项 / 巡检禁用 M 项」的构成。

### 📋 设置页「关于」

设置页最底部有一块「关于」，用来回答「我装的到底是什么」：项目名称与版本、
项目主页 / 更新日志 / 发布版本 / 问题反馈入口、容器镜像地址（可一键复制）、
数据目录与端口、当前规模（账号 / 模型 / 禁用组合，按来源拆分），以及非官方项目声明。

项目地址与镜像名由服务端下发，可用环境变量覆盖 —— 二次分发的版本不会把使用者引回本仓库。

### 🔌 全量模型 OpenAI 兼容网关

- 上游全系列主流大模型与官方工作模式，全部转为标准 OpenAI 格式。
- **模型清单自动发现**：官方未提供模型列表接口，网关改从实际调用流水与实测统计中自动聚合，
  新模型上线后无需手工补清单。
- 支持 `stream: true` 与 `stream: false` 自动双向转换。
- 自动补全系统级 Prompt，保障上游稳定响应。

### 🖥️ 为容器重做的控制台

- **桌面专属功能物理移除**：账号卡片上的桌面产品槽位、桌面程序状态图标、导入本机账号按钮，
  以及设置页的权限检测 / 启动设置 / 自动更新 / 限额监听等区块，全部在源码层面移除 ——
  它们要么依赖宿主桌面客户端，要么在容器里靠镜像重建升级，留在界面上只会产生死按钮与误导。
- **侧边栏可折叠**：宽窄两种模式智能切换，并记忆状态。
- **Token 统计全部面板都有真实数据**：用量分布、消耗最高的调用、趋势图按模型筛选，
  由自研网关实时统计与官方云端历史融合，告别空白报表。
- **文案按容器能力适配**：官方前端是给桌面写的，代理层对首页副标题、统计页维度标签等做
  等长字节替换，既改对语义又不破坏前端资源的偏移与语法。
- **响应禁缓存**：前端资源文件名带 hash 不随改动变化，代理强制 HTML / JS 响应不使用缓存，
  避免浏览器拿旧副本导致「改了却没生效」。
- **全链路图标高清对齐**：侧边栏、顶栏与浏览器标签页统一使用高清圆角图标。

### 📊 Token 统计的口径说明

网关自身记录的是**实测估算** Token（按文本长度估算）；从官方云端流水合并进来的记录只有「积分」，
其 Token 是按固定比例**折算的估算值**。两者会出现在同一张报表里，判断绝对量级时请注意这一差异。

---

## 🚀 快速部署

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

数据卷：

- `/data` —— 挂载至宿主机的 AppData 目录（如 `/mnt/user/appdata/workbuddy-switch`），
  持久化保存账号凭证、积分快照、Token 统计明细、账号池与模型策略配置、API 密钥。
- 删除 `/data/.wb-switch/api_keys.json` 即等于关闭密钥校验并清空全部密钥。

---

## 🔑 API 接入

容器的第二个核心能力是「对外提供 OpenAI 兼容 API」。

### 从零接一个下游客户端

1. 打开 `http://<NAS-IP>:18090` → **设置** → **API 接入**；
2. 点 **新建密钥**，起个名字（例如 `Sub2API`），密钥会自动复制到剪贴板；
3. 把面板里显示的 **对外访问地址** 填进客户端的 `base_url`，把密钥填进 `api_key`。

> 只在内网使用且不希望增加配置成本时，保持校验关闭即可；
> 一旦端口暴露到公网或经过隧道转发，**务必打开校验**。

### 密钥规则

- 格式 `sk-wb-` + 32 位十六进制，放在 `Authorization: Bearer <key>` 或 `x-api-key: <key>` 均可，
  `Bearer` 大小写不敏感，首尾空格会被容忍。
- **默认不校验**：没有配置密钥的用户，行为与不设密钥时完全一致。
- 缺失 / 错误 / 已停用的密钥统一返回 `401`，并给出可读中文原因。
- **容器内回环免校验**：WebUI 代理与网关同容器，它要读模型清单来渲染账号卡片，
  不能被自己的密钥挡住。容器外的请求经 docker NAT 进来，源地址是网桥地址而非 `127.0.0.1`，
  因此放行回环是安全的。

### Sub2API 配置

1. **渠道（Channel）**：选择 `OpenAI` 格式，Base URL 填入 `http://<IP>:18091/v1`。
2. **账号类型**：选择 `apikey`。若已打开 **强制 API 密钥校验**，这里必须填真实密钥（`sk-wb-…`）；
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
| `GET /account-pool/selections` · `POST /account-pool/selections/reset` | 并发分摊观测与清零 |
| `GET /account-models/config` · `PUT /account-models/config` | 模型级禁用策略 |
| `GET /model-health/config` · `PUT /model-health/config` · `GET /model-health/status` · `POST /model-health/run` | 模型可用性巡检：配置、状态与手动触发 |
| `GET /api-keys/status` · `POST /api-keys` · `POST /api-keys/update` · `POST /api-keys/delete` · `POST /api-keys/delete-all` · `PUT /api-keys/config` | 密钥管理 |

> **鉴权范围**：密钥校验只作用于 `/v1/*`。其余管理接口不拦截，它们只供本机 WebUI 与运维使用 ——
> 因此**不要把 18091 直接暴露到公网**，需要外网访问请用 WebUI（18090）或前置反向代理并自行加认证。

### 控制台接口（`18090`）

控制台通过 `/api/*` 前缀转发上述管理能力，供设置页与账号卡片使用
（如 `/api/gateway-info`、`/api/api-keys`、`/api/account-models`、`/api/model-health`、`/api/gateway-selftest`）。

### 账号池配置

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

## 🔧 容器化补丁机制（维护须知）

官方核心服务是单文件二进制，前端界面代码内嵌其中，无法直接改源码。
本项目用 `patch/patch_binary.py` 对它做**等长字节替换**（改动字节数必须完全一致，否则会破坏二进制内的偏移表）。

两条纪律，改动补丁时务必遵守：

1. **锚点不得依赖压缩变量名**。官方每次发版都会重排压缩变量名，因此锚点一律用稳定的
   **文案 / 类名 / 元素 id** 定位，再回溯到调用起点，变量名从匹配结果里现取。
2. **锚点失配必须让构建失败**。补丁脚本会收集所有失败项并以非零码退出，
   构建流程里没有兜底吞错，因此锚点一旦失配镜像构建就会失败，不会静默上线。

### 升级上游版本

```bash
# 1. 拉一份新版本二进制，本地先验证锚点
npm pack workbuddy-switch-linux-x64@<新版本> && tar xzf workbuddy-switch-linux-x64-*.tgz
python3 patch/patch_binary.py package/bin/wb-switch-linux-x64

# 2. 全部 ok 后再改 docker/Dockerfile 里的 WB_SWITCH_VERSION，然后构建
```

若某条锚点报 `FAIL`，说明官方前端结构变了：用 `grep -a -o -E '.{200}<原锚点片段>.{200}' <二进制>`
看清新结构，再更新对应锚点。

---

## 📄 更新历史与许可

- **每个版本改了什么** → [CHANGELOG.md](./CHANGELOG.md)
- **版本索引与部署产物清单** → [RELEASES.md](./RELEASES.md)
- **各版本完整发布说明** → [docs/release-notes/](./docs/release-notes/)
- 它怎么变成现在这样的，看 CHANGELOG；怎么用起来，看这份 README。
- 本项目基于 [MIT 协议](LICENSE) 开源。

> 维护提示：改了 CHANGELOG 后请跑 `python3 scripts/gen_releases.py` 重新生成版本索引，避免两处漂移。
