# AutoBuddy

多账号统一管理面板与 OpenAI 兼容 API 网关，面向 NAS 与服务器部署。

将 WorkBuddy / CodeBuddy 客户端的账号管理、签到保活与模型调用能力容器化，
对外提供标准 OpenAI 接口，并补齐多账号并发分摊、账号 × 模型能力矩阵、
可用性巡检等单机客户端不具备的能力。

<p>
  <a href="https://github.com/deltrivx/AutoBuddy/releases"><img src="https://img.shields.io/github/v/release/deltrivx/AutoBuddy?display_name=tag&sort=semver&label=Release" alt="Release" /></a>
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white" alt="Docker" />
  <img src="https://img.shields.io/badge/Unraid-Compatible-F15A24?logo=unraid&logoColor=white" alt="Unraid" />
  <img src="https://img.shields.io/badge/FastAPI-Gateway-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/OpenAI_API-Compatible-412991?logo=openai&logoColor=white" alt="OpenAI API" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License" /></a>
</p>

[功能](#功能) · [快速开始](#快速开始) · [配置](#配置) · [接入](#接入) ·
[运维](#运维) · [实现说明](#实现说明) · [许可](#许可) · [致谢](#致谢) ·
[更新日志](CHANGELOG.md) · [版本索引](RELEASES.md)

---

## 功能

### 账号池

- **请求级分摊**：每次请求独立分配账号，上下文与计费互不串扰。
- **并发优先空闲**：按「在飞请求数最少」挑选账号，避免慢请求集中在同一账号。
- **账号级控制**：启用 / 停用、设为首选（固定单一账号）、检测账号（轻量鉴权探活）。
- **停用来源区分**：手动停用与巡检自动停用在界面以不同配色呈现，可分辨归属。

### 账号 × 模型能力矩阵

上游部分模型仅对特定账号开放，直接调用会返回模型不存在的错误。

- **负向学习**：识别上游「账号不支持该模型」响应（400/403/404 及错误码
  `11102`/`11103` 等），记录该组合并自动换号重试，重试次数上限等于候选账号数。
  重试发生在响应体转发给客户端之前，对调用方透明；流式与非流式路径均覆盖。
- **正向学习**：同时记录调用成功的组合，选号时优先选择已知支持的账号。
  仅在确实存在支持者时才收窄候选集，避免矩阵过期导致账号池被整体排除。
- **手动禁用优先**：界面点击禁用记为该组合的长期策略，巡检不会自动放开；
  巡检因探测失败自动禁用的条目会单独标注，并在恢复后自动解除。
- **批量恢复**：账号卡片提供「全部恢复」，一次性放回该账号所有被禁用的模型。

### 可用性巡检

后台独立线程定期探测各账号 × 模型组合，结论分为四种状态：

| 状态 | 含义 | 处理 |
| :--- | :--- | :--- |
| `valid` | 账号可用 | 正常参与调度 |
| `invalid` | 凭据失效 | 需重新登录 |
| `restricted` | 凭据有效但请求被上游拦截 | 重新登录无效 |
| `unknown` | 探测过程本身异常 | 结论不定，需复检 |

`transient`、`probe_defect`、`restricted` 三种探测结果不写入能力矩阵，
只有明确的 `available` / `unavailable` 才回填，避免把探测侧问题记为模型不支持。

设置页「一致性自检」并列展示各账号的凭据结论与模型结论，同时列出判定依据，
用于区分「凭据有效但被拦截」与「模型无权限」两种情况。

### 每日任务

内置 vendor 化的签到脚本，由容器内独立服务托管执行。

- **任务类型**：积分与成长查询、成长任务、互动玩法、活动与自动领奖。
- **执行模式**：`完整任务`（执行签到与领奖）与 `仅查询`（只读，不改变任何任务状态）。
- **调度**：可配置执行间隔与保活阈值，启动时校验服务端状态并补签未完成的账号。
- **账号来源**：直接读取账号池中的国内版账号，无需单独登记。
- **凭据处理**：只读使用账号池的 refresh token，不写回、不覆盖账号池文件。

### API 网关

- 提供 `/v1/chat/completions`（支持流式）与 `/v1/models`，兼容标准 OpenAI 客户端。
- 上游模型清单自动发现并映射，无需手工维护。
- 可选的 Bearer 密钥校验，支持密钥新建、启停、删除、IP 白名单与连通性自检。
- 按账号与模型聚合 Token 用量与调用记录。

### 界面

- 账号资料：头像（官方图标、内置预设、自定义上传）。
- 右下角用户球：显示当前用户与积分、当日消耗、账号池规模。
- 窄屏适配：宽度不足时侧边栏自动收起。
- 已移除依赖宿主桌面客户端的入口（文件管理器、权限检测、IDE / CLI 切换等）。

---

## 快速开始

镜像通过 GitHub Actions 构建并推送至 GHCR，不在本地构建
（本地构建会跳过 CI 中的单元测试门禁）。

### Unraid 模板

1. Docker 页面 → **Template Repositories** 添加本仓库地址并刷新；
2. 或下载 [unraid/autobuddy.xml](./unraid/autobuddy.xml) 放入
   `/boot/…ates-user/`；
3. **Add Container** → 选择 `AutoBuddy` → 确认端口与数据目录 → 应用。

升级：在模板页面执行 **Force Update**，容器重建后数据保留。

### Docker Compose

```bash
docker compose pull && docker compose up -d
```

完整示例见 [docker-compose.yml](./docker-compose.yml)。

### Docker CLI

```bash
docker run -d \
  --name AutoBuddy \
  -p 18090:18090 \
  -p 18091:18091 \
  -v /mnt/user/appdata/autobuddy/data:/data \
  -e TZ=Asia/Shanghai \
  -e AUTH_USERNAME=admin \
  -e AUTH_PASSWORD=<你的密码> \
  --restart unless-stopped \
  ghcr.io/deltrivx/autobuddy:latest
```

启动后访问 `http://<服务器IP>:18090` 进入管理面板。

---

## 配置

### 端口

| 容器端口 | 默认宿主端口 | 协议 | 用途 |
| :--- | :--- | :--- | :--- |
| `18090` | `18090` | HTTP | 管理面板 |
| `18091` | `18091` | HTTP | OpenAI 兼容 API |
| `18093` | 不映射 | HTTP | 每日任务服务（仅容器内 loopback） |

### 持久化

| 容器路径 | 推荐宿主路径 | 内容 |
| :--- | :--- | :--- |
| `/data` | `/mnt/user/appdata/autobuddy/data` | 数据库、账号凭据、模型策略、用量记录、头像。升级与重建容器不丢失。 |

### 环境变量

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `AUTH_USERNAME` | `admin` | 面板登录用户名。设置后界面锁定，需改环境变量并重建容器。 |
| `AUTH_PASSWORD` | `password` | 面板登录密码。同上。 |
| `AUTOBUDDY_SESSION_HOURS` | `24` | 登录会话有效期（小时）。 |
| `TZ` | `Asia/Shanghai` | 时区，影响签到调度与用量按日归集。 |
| `PORT` | `18090` | 面板端口，修改时需同步调整端口映射。 |
| `API_PORT` | `18091` | 网关端口，同上。 |
| `WB_DAILY_ENABLED` | `1` | 是否启用每日任务服务。 |
| `WB_DAILY_PORT` | `18093` | 每日任务服务端口（仅容器内）。 |

---

## 接入

```
Base URL: http://<服务器IP>:18091/v1
API Key:  面板「设置 → API 接入」中创建的密钥
```

未开启密钥校验时，API Key 可填写任意非空值。

```bash
curl -X POST http://<服务器IP>:18091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <API Key>" \
  -d '{
    "model": "<模型名>",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

可用模型通过 `GET /v1/models` 获取，也可在面板的模型策略页面查看。

---

## 运维

### 健康检查

```bash
curl -fsS http://<服务器IP>:18091/health
```

返回 `status`、`version`、当前活跃账号与轮换状态，可直接用作容器 healthcheck。

### 日志

日志经过两层过滤，仅保留有效信息：

1. **访问日志过滤**：健康探活、面板定时轮询、静态资源等成功请求不输出；
   4xx 与 5xx 响应、业务请求（如 `/v1/chat/completions`）全部保留。
2. **业务日志去重**：账号选号、轮换、巡检等高频日志与上一条相同时只累计计数，
   内容变化时补输出重复次数，不丢失观测信息。

```bash
docker logs -f AutoBuddy
docker logs --tail 200 AutoBuddy
```

### 常见问题

| 现象 | 处理 |
| :--- | :--- |
| 下游返回 `model not found` | 该模型当前无可用账号。首次调用会自动学习并换号重试；仍失败则说明账号池中确无支持者，可在模型策略页面查看。 |
| 某账号持续调用失败 | 在账号卡片执行「检测账号」。红色表示凭据失效，需重新登录；琥珀色表示被上游拦截，重新登录无效。 |
| 界面修改后未生效 | 服务端已禁用缓存。执行强制刷新（Ctrl / Cmd + Shift + R）。 |
| 忘记面板密码 | 若由环境变量设置，修改 `AUTH_PASSWORD` 后重建容器。 |

---

## 实现说明

### 结构

```
gateway/
  main.py            API 网关（:18091）：账号调度、账号×模型矩阵、巡检
  web_proxy.py       管理面板反向代理与界面注入（:18090）
  wb_daily.py        每日任务服务（:18093，仅容器内）
  db.py              SQLite 持久化
  vendor/            上游签到脚本（vendor 化）
patch/
  patch_binary.py    对上游二进制打补丁，移除桌面客户端专有入口
docker/
  Dockerfile         镜像构建定义
unraid/
  autobuddy.xml      Unraid 容器模板
_test_*.py           单元测试（CI 在构建前执行）
```

### 界面注入

管理面板在上游前端的基础上做增量修改，而非重写：

- 反向代理在 HTML 响应中注入脚本，追加「每日任务」页面与设置页各功能区块；
- 在 JS 资源中替换 UI 文案，统一品牌与命名；
- 上游桌面前端专有的入口在 `patch_binary.py` 中于二进制层面移除，
  注入脚本仅做兜底清理。

注入脚本若存在语法错误会导致整段不执行、界面上相关区块静默消失，
因此 `_test_webui_panels.py` 会对注入脚本执行 `node --check` 语法校验。

### 构建约束

- 上游二进制版本在 Dockerfile 中显式锁定。`patch_binary.py` 的匹配锚点依赖
  上游前端的具体结构，上游发版可能重排压缩变量名或改动 DOM，锚点失配时构建失败，
  需人工复核后更新锚点，不可直接跟随 `latest`。
- 镜像不在本地构建，统一走 CI。

---

## 许可

[MIT License](LICENSE)

## 致谢

本项目基于以下工作：

- **[WorkBuddy-Switch](https://www.npmjs.com/package/workbuddy-switch)**
  提供底层能力与前端界面。AutoBuddy 对其做容器化、网关化改造，
  并实现多账号池、账号 × 模型能力矩阵、可用性巡检与 OpenAI 兼容网关。
- **[L0NE-6/WorkBuddy-Daily](https://github.com/L0NE-6/WorkBuddy-Daily)**（MIT）
  提供每日成长任务脚本，以 vendor 形式内置。
- **[FastAPI](https://fastapi.tiangolo.com/)**、**[Uvicorn](https://www.uvicorn.org/)**、
  **[HTTPX](https://www.python-httpx.org/)** 构成本项目的服务端技术栈。
- **GitHub Actions** 与 **GHCR** 提供镜像构建与托管。
