# AutoBuddy

<p align="center">
  <a href="https://github.com/deltrivx/AutoBuddy">
    <img src="./icon.png" width="120" height="120" alt="AutoBuddy Logo" style="border-radius: 28px; box-shadow: 0 8px 24px rgba(0,0,0,0.12);" />
  </a>
</p>

<p align="center">
  <strong>多账号统一管理面板 · OpenAI 兼容 API 网关 · 定时签到保活</strong>
</p>

<p align="center">
  <a href="https://github.com/deltrivx/AutoBuddy/releases"><img src="https://img.shields.io/github/v/release/deltrivx/AutoBuddy?display_name=tag&sort=semver&label=Release" alt="最新版本" /></a>
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/Unraid-Compatible-F15A24?logo=unraid&logoColor=white" alt="Unraid" />
  <img src="https://img.shields.io/badge/FastAPI-Gateway-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/OpenAI_API-Compatible-412991?logo=openai&logoColor=white" alt="OpenAI API" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License" /></a>
</p>

<p align="center">
  <a href="#-这个项目解决什么问题">项目定位</a> ·
  <a href="#-功能详解">功能详解</a> ·
  <a href="#-部署方式">部署方式</a> ·
  <a href="#-端口与持久化">端口与持久化</a> ·
  <a href="#-接入与调用">接入与调用</a> ·
  <a href="#-运维与排障">运维与排障</a> ·
  <a href="CHANGELOG.md">更新日志</a> ·
  <a href="RELEASES.md">版本索引</a>
</p>

---

## 📖 这个项目解决什么问题

WorkBuddy / CodeBuddy 官方客户端在单机上只能登录**一个**账号，额度用尽就得手动换号；
官方能力也没有标准 API 接口，没法直接喂给自己常用的 AI 客户端。

AutoBuddy 把官方客户端能力搬到容器里跑，解决三件事：

| 痛点 | AutoBuddy 的做法 |
| :--- | :--- |
| 单账号额度不够、手动换号麻烦 | **多账号账号池**：请求级自动分摊，并发时优先挑最空闲的账号 |
| 有些账号不支持某些模型，一调就报 `model not found` | **账号 × 模型 双向学习**：调通了记住、被拒了记住，下次自动绕开 |
| 官方没有 API，接不进自己的客户端 | **OpenAI 兼容网关**：输出标准 `/v1/chat/completions`、`/v1/models`，支持流式 |
| 账号长期不用会被回收，每天手动点签到 | **定时签到保活**：内置每日成长任务自动化，按间隔自动跑 |
| 某个账号登录过期了没人知道，直到调用失败 | **可用性巡检**：后台定期探活，区分「凭据失效」与「模型不可用」 |

> 本项目是 [WorkBuddy-Switch](https://www.npmjs.com/package/workbuddy-switch) 的**容器化 + 网关化**改造，
> 并非从零实现。上游项目与开源依赖的致谢见文末。

---

## ⚡ 功能详解

### 1. 账号池：多账号自动分摊

- **请求级轮询**：每次请求独立分配账号，上下文与计费互不串扰（不会串号）。
- **并发智能分摊**：按「在飞请求数最少」挑账号 —— 并发请求自动落到最闲的账号，
  不会因为慢请求全压在同一个人身上。状态接口的 `inflight` 字段可实时查看各账号在飞数。
- **手动干预**：
  - 账号级 **启用 / 停用** —— 停用后不参与自动轮询（显式指定它的请求仍可用）；
  - **设为首选** —— 固定只用某一个账号，适合「先把某个号跑满」的场景；
  - **检测账号** —— 发一次轻量鉴权请求，就地报告这个账号现在能不能用（不改配置）。
- **两种「不可用」分开说**：手动停用（灰）与巡检自动停用（琥珀）在界面上颜色不同，
  一眼能看出「这不是我点的」，恢复方式也明确标注。

### 2. 模型级控制：账号 × 模型 能力矩阵

这是项目的核心差异点。上游很多模型**只有部分账号有权**，默认行为是一调用就报错。

- **负向学习**：上游返回「该账号不支持该模型」时（状态码 400/403/404 + 错误码
  `11102`/`11103` + `service info not found` 等一揽子文案），立即把该组合写入 auto 拉黑，
  **并自动换下一个账号重试**（上限 = 候选账号数），对客户端完全透明。
- **正向学习**：不止事后拉黑 —— 正向记录「某账号用过某模型且成功」。选号时优先挑
  「已知支持」的账号，且**只在确实存在支持者时才收窄候选集**，避免矩阵过旧把整个账号池排除掉。
- **界面上直接点**：账号卡片下方列出该账号的可用模型标签，点一下禁用、再点恢复；
  被巡检自动禁用的用不同颜色标出。**手动禁用不会被巡检自动放开** —— 你关的就是你关的。
- **一键恢复**：卡片上显示「已禁用 N / 巡检 N」两个互不重叠的计数，
  以及一个「全部恢复」按钮，不必逐个点标签。

### 3. 可用性巡检：把「凭据坏」和「模型不支持」分开

后台独立轻量线程定期探活，结论明确区分四种状态：

| 状态 | 含义 | 界面配色 |
| :--- | :--- | :--- |
| `valid` | 账号可用 | 绿 |
| `invalid` | 凭据失效，需要重新登录 | 红 |
| `restricted` | 凭据是好的，但请求被上游拦截 | 琥珀 |
| `unknown` | 探测侧问题，结论不确定 | 琥珀 |

这一点很重要：**受限账号（restricted）的凭据其实是好的**，如果一律标红，
用户会以为凭据坏了跑去重新登录 —— 而重登对这种情况毫无帮助。

- **不会把探测侧问题伪装成模型不支持**：`transient` / `probe_defect` / `restricted`
  三种探测结果**不写入**能力矩阵，只有真正的 `available` / `unavailable` 才回填。
- **一致性自检**：把每个账号的「凭据结论」与「模型结论」并排摆出来，
  连同**判据与建议**一起列出，消除「检测说有效、巡检说失效」这种口径矛盾。

### 4. 每日任务：签到与成长自动化

内置 vendor 化的上游开源脚本（[L0NE-6/WorkBuddy-Daily](https://github.com/L0NE-6/WorkBuddy-Daily)，MIT），
由本服务托管执行，侧边栏「每日任务」页统一配置：

- **四种任务**：积分与成长查询、成长任务、互动玩法、开学季活动与自动领奖；
- **两种模式**：`完整任务`（签到 / 玩法 / 领奖）与 `仅查询`（只读积分与用量，不产生任何任务状态变更）；
- **定时调度**：可设执行间隔（小时）与保活阈值，启动时立即核验服务端状态，未签到账号自动补签；
- **账号自动关联**：直接读取账号池里的**国内版（cn）账号**，新增账号无需在此登记 ——
  账号池就是唯一权威来源，不会出现「哪些账号在跑」有两个答案的情况；
- **凭据只读共用**：用账号池的 refresh_token 生成脚本所需凭据，
  **绝不写回、绝不覆盖**账号池文件（实测续期后旧令牌仍有效，无烧号风险）；
- **进度可视化**：执行中显示阶段明细与进度条，可中止；账号签到与凭据状态一账号一行展示。

### 5. OpenAI 兼容网关

- **标准接口**：`/v1/chat/completions`（支持流式非流式）与 `/v1/models`，
  直接对接 Sub2API / Cherry Studio / NextChat / LobeChat 等任何 OpenAI 兼容客户端。
- **模型目录自动发现**：上游模型清单自动映射，无需手写；`/v1/models` 实时反映可用模型。
- **API 访问密钥**：可选的 Bearer 密钥校验，支持密钥新建 / 启停 / 删除、
  白名单、**连通性回环自检**与配额统计。
- **Token 用量统计**：按账号与模型聚合 Token 消耗与调用流水。

### 6. 容器专属控制台

- **账号资料**：头像（官方图标 / 6 款预设 / 自定义上传）、昵称、用户名与密码修改；
- **右下角用户球**：44px 圆形头像，悬浮或点击展开浮窗，显示剩余积分 / 今日消耗 / 账号池规模；
- **侧边栏自动收起**：窄屏（≤720px）自动收起侧栏，避免在手机上吃掉一半屏幕；
- **移除桌面依赖**：宿主桌面程序专有的入口（Finder、完全磁盘访问、IDE / CLI 切换等）
  已在二进制层面物理移除，容器内不再出现点了没反应的按钮；
- **响应禁用缓存**：注入脚本与文案替换全部 `no-store`，避免「改了但没生效」。

---

## 🚀 部署方式

> 容器镜像全部通过 GitHub Actions **云端自动化构建**并推送至 GitHub Packages (GHCR)，
> 严禁本地私有构建（本地构建会跳过 CI 里的单测门禁）。

### 方式一：Unraid 容器模板（强烈推荐）

1. Unraid 控制台 → **Docker** → 底部 **Template Repositories** 填入本仓库地址并刷新；
2. 或直接下载模板文件 [unraid/autobuddy.xml](./unraid/autobuddy.xml)，
   放到 `/boot/config/plugins/dockerMan/templates-user/` 下；
3. **Add Container** → 选择 `AutoBuddy` 模板 → 确认端口与 AppData 路径 → 应用。

后续升级：在模板页点 **Force Update** 即可拉取最新镜像重建容器，数据不受影响。

### 方式二：Docker Compose

```yaml
services:
  autobuddy:
    image: ghcr.io/deltrivx/autobuddy:latest
    container_name: AutoBuddy
    restart: unless-stopped
    ports:
      - "18090:18090"   # Web 控制面板
      - "18091:18091"   # OpenAI API 网关
    volumes:
      - /mnt/user/appdata/autobuddy/data:/data
    environment:
      TZ: Asia/Shanghai
      AUTH_USERNAME: admin        # 控制台登录账号
      AUTH_PASSWORD: change-me    # 控制台登录密码（务必修改）
```

```bash
docker compose pull && docker compose up -d
```

> 完整可运行示例见仓库根目录 [docker-compose.yml](./docker-compose.yml)。

### 方式三：Docker CLI

```bash
docker run -d \
  --name AutoBuddy \
  -p 18090:18090 \
  -p 18091:18091 \
  -v /mnt/user/appdata/autobuddy/data:/data \
  -e TZ=Asia/Shanghai \
  -e AUTH_USERNAME=admin \
  -e AUTH_PASSWORD=change-me \
  --restart unless-stopped \
  ghcr.io/deltrivx/autobuddy:latest
```

---

## 📂 端口与持久化

### 端口

| 容器内端口 | 默认宿主端口 | 协议 | 用途 |
| :--- | :--- | :--- | :--- |
| `18090` | `18090` | HTTP | **Web 控制面板** —— 账号管理、每日任务、模型策略、API 密钥 |
| `18091` | `18091` | HTTP | **OpenAI API 网关** —— 标准 `/v1` 接口，对接下游客户端 |
| `18093` | 不对外 | HTTP | 每日任务内部服务（仅容器内 loopback，由面板转发） |

### 持久化

| 容器路径 | 推荐宿主路径 | 说明 |
| :--- | :--- | :--- |
| `/data` | `/mnt/user/appdata/autobuddy/data` | **唯一需要挂载的目录**：SQLite 数据库、账号凭据、模型策略、用量流水、头像等全部在这里。升级 / 重建容器不丢数据。 |

### 环境变量

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `AUTH_USERNAME` | `admin` | 控制台登录账号。**设置后界面上锁定为不可修改**，需改环境变量并重建容器。 |
| `AUTH_PASSWORD` | `password` | 控制台登录密码。同样在设置后锁定。 |
| `AUTOBUDDY_SESSION_HOURS` | `24` | 登录会话有效期（小时）。 |
| `TZ` | `Asia/Shanghai` | 时区，影响签到调度与用量按日归集。 |
| `PORT` / `API_PORT` | `18090` / `18091` | 服务端口，改这里要同步改端口映射。 |
| `WB_DAILY_ENABLED` / `WB_DAILY_PORT` | `1` / `18093` | 每日任务服务开关与端口。 |

---

## 🔌 接入与调用

### 下游客户端配置

- **Base URL**：`http://<服务器IP>:18091/v1`
- **API Key**：控制台【设置】→【API 接入】中创建的密钥
  （未开启强制校验时可随意填写）

### cURL 快速测试

```bash
curl -X POST http://localhost:18091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的密钥>" \
  -d '{
    "model": "<模型名>",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

模型清单可通过 `GET /v1/models` 获取，或在控制台的模型策略页查看。

---

## 🔧 运维与排障

### 健康检查

```bash
curl -fsS http://<服务器IP>:18091/health
```

返回 `status`、`version`、当前活跃账号与轮询状态，可直接用于容器 healthcheck。

### 后台日志

日志经过**两层降噪**，只留有用信息：

1. **access log 过滤**：健康探活、WebUI 定时轮询、静态资源等成功请求不再刷屏；
   **4xx / 5xx 一律保留线索**，真实业务请求（如 `/v1/chat/completions`）全部保留。
2. **业务日志去重**：账号选号、轮换、巡检等高频日志若与上一条完全相同，
   只累计次数不重复输出；出现不同内容时先补一行 `(上一条重复 N 次)`，观测不断档。

```bash
docker logs -f AutoBuddy          # 实时跟踪
docker logs --tail 200 AutoBuddy  # 看最近 200 行
```

### 常见问题

| 现象 | 原因与处理 |
| :--- | :--- |
| 下游报 `model not found` | 该模型当前没有任何账号支持。到模型策略页查看；首次调用会自动学习并重试，若仍失败说明确实无可用账号。 |
| 某个账号反复失败 | 到该账号卡片点 **检测账号** 看结论：红=凭据失效需重新登录，琥珀=被上游拦截（重登无用）。 |
| 界面改了没生效 | 浏览器缓存了旧资源。服务端已 `no-store`，强制刷新（Ctrl/Cmd + Shift + R）即可。 |
| 忘记控制台密码 | 若通过环境变量设置，修改 `AUTH_PASSWORD` 后重建容器；否则删除 `/data` 下认证记录重新初始化。 |

---

## 📄 版本与许可

- **更新日志**：[CHANGELOG.md](CHANGELOG.md)
- **版本索引**：[RELEASES.md](RELEASES.md)
- **开源协议**：[MIT License](LICENSE)

---

## 🙏 致谢

AutoBuddy 站在以下项目之上，特此致谢：

- **[WorkBuddy-Switch](https://www.npmjs.com/package/workbuddy-switch)** —— 本项目的基础。
  AutoBuddy 将其原生客户端能力（账号管理、Token 刷新、前端界面）容器化，
  并补上多账号池、模型能力矩阵、可用性巡检、OpenAI 兼容网关等 NAS / 服务器场景所需能力。
  底层二进制通过 npm 安装，版本在 Dockerfile 中**显式锁定**（不打补丁锚点会失配）。
- **[L0NE-6/WorkBuddy-Daily](https://github.com/L0NE-6/WorkBuddy-Daily)**（MIT）——
  每日成长任务签到脚本，以 vendor 形式内置，由本项目的每日任务服务托管执行。
- **[FastAPI](https://fastapi.tiangolo.com/)** / **[Uvicorn](https://www.uvicorn.org/)** /
  **[HTTPX](https://www.python-httpx.org/)** —— 网关与面板服务的技术栈。
- **GitHub Actions / GHCR** —— 提供免费的多架构镜像构建与托管。
- 以及所有账号管理、代理与自动化领域的开源作者。

如有遗漏或希望调整署名方式，欢迎提 Issue。
