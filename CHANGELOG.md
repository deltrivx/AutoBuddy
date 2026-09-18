# 更新日志 (Changelog)

本项目严格遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 规范。

---

## [v0.3.9] - 2026-09-18

### 🖥️ 让 CodeBuddy CLI 在容器里真正可用（从「空转配置」到「可执行环境」）

本版回答并落地了一个此前含糊的问题：**CodeBuddy CLI 助手在容器里有意义吗？**

#### 1. 结论：CLI 不是「仅适用于系统环境」，官方定位就是容器 / 无头

深度检测结论：CodeBuddy CLI 官方明确支持在 **Docker 容器 / CI/CD runner / 远程服务器**中运行（官方原话：不依赖图形界面，可在无头环境中正常运行），且 `apiKeyHelper` 是**官方支持的 settings 配置项**（脚本在 `/bin/sh` 执行，输出作为 `X-Api-Key` 与 `Authorization: Bearer`）。因此不能按「仅桌面」移除，而应补全为真正可用的容器环境。

#### 2. 镜像内置 CodeBuddy CLI
- `docker/Dockerfile` 新增 `npm i -g @tencent-ai/codebuddy-code`（Node 20 满足其 18.20+ 要求），并设置 `ENV DISABLE_AUTOUPDATER=1`（容器内自动更新会在重建时丢失且拖慢启动）。
- 安装 `git`（CLI 的版本控制能力依赖它），并预留 `/workspace` 作为默认工作目录。
- 镜像体积相应增加约 175 MB（CLI 解压后大小），这是「容器内真的能跑 CLI」的代价。

#### 3. 修复 `state.json` 缺失导致的绑定账号不确定
- **问题**：`helper.cjs` 依赖 `~/.codebuddy-rotate/state.json` 的 `activeAccountId` 决定给 CLI 用哪个账号；该文件此前**不存在**，helper 只能 fallback 到 `accounts[0]`，行为不确定，且与面板显示不一致。
- **修复**：新增 `gateway/cli_bootstrap.py`，容器启动时（`entrypoint.sh` 步骤 2b）在 `state.json` 缺失的情况下，调用官方 `POST /api/codebuddy-cli/switch` 绑定一个账号，由 wb-switch 自己写出格式正确的 `state.json`。优先选国际版 `variant=ai` 的账号，与默认端点 `codebuddy.ai` 匹配。
- **不覆盖已存在的 `state.json`**，因此不会影响用户手动选择或后续的自动轮换。

#### 4. UI：消除「假接入」，显示真实可用性
- **新增 `GET /api/cli-info`**：真实探测容器内 `codebuddy` 是否可执行并返回版本（结果缓存 60 秒）。原因是官方「已接入」判据 `codebuddyCliConfigured` **只看配置文件是否存在，从不检测 CLI 二进制**，容器里没装 CLI 也会显示「已接入」。
- 设置页「CodeBuddy CLI 自动轮换」区块的说明条重写：说明容器内已安装 CLI、非交互用法 `codebuddy -p '…' -y`，并**实时显示探测到的 CLI 版本**（可用为绿色、未检测到为橙色）。
- 账号卡片 `官方 CLI 绑定` 标记的悬停说明同步改写，明确它决定的是 CLI 用哪个账号。

#### 5. 文档
- README 新增「🖥️ 容器内使用 CodeBuddy CLI」章节：认证链路（`settings.json` → `apiKeyHelper` → `helper.cjs` → `state.json`）、交互式与无头用法、`/workspace` 挂载建议，以及「CLI 绑定账号 vs 网关账号池」职责对照表。
- 修正 README 中两处已过时表述（原写「容器里已无 IDE / CLI 能力」「容器里没有真实 CLI 会话」）。
- 数据卷说明补充 `/workspace`（可选）。

---

## [v0.3.8] - 2026-09-18

### 🧭 全局巡检收尾：把「半成品面板」和「说不清的状态」一次清掉

本版是针对「希望这是一个可以使用的完整项目，而不是我说一处你改一处」的一次系统性排查与修复。

#### 1. 账号卡片：移除冗余提示，保留有效信息
- 删除卡片上的 `自动分配中，并发请求会分摊到 N 个已启用账号` 提示文案与 `清零统计` 按钮（观测入口仍保留在 `/account-pool/selections`）。
- 保留 `已调用 N 次` 计数徽章与 `参与调用` / `设为首选` 控件。

#### 2. 「当前 CLI 账号」只有一个账号的原因与处理
- **原因**：设置页的「当前 CLI 账号」来自官方底层 `/api/codebuddy-cli/status`，官方只维护**一个** `activeAccountId`（用于绑定 CodeBuddy CLI 助手），本质上是单账号概念。
- **处理**：
  - 设置页「CodeBuddy CLI 自动轮换」区块顶部注入范围说明，讲清它只影响 CLI 助手、不影响网关 API 调用。
  - 账号卡片给被绑定的那张卡打 `官方 CLI 绑定` 虚线标记（`/api/account-pool` 已并入 `cliActiveAccountId`），不再看起来「只有它有状态」。

#### 3. 其他账号看不到模型调用提示（根因修复）
- **根因**：`/api/account-models` 只读上游 `official_usage_cache.json`，而上游**只对当前账号返回模型明细**（其余账号 `daily[].models` 恒为空），于是 5 个账号里只有 1～2 个有调用记录。
- **修复**：网关每次调用都把实际服务该请求的账号写进 `token_stats_logs.json`（新增 `accountId` / `accountName` / `variant`），`/api/account-models` 以**网关归因为主数据源**、上游缓存为补充。实测 5 个账号全部显示各自的已调用模型。
- 悬停标签同时给出「网关调用次数 / 估算 Token / 官方记录次数 / 消耗积分」。

#### 4. Token 统计页两个面板恒为空（根因修复）
- **根因**：后端 `make_source_obj()` 里 `projects: []`、`sessions: []`、`dailyByModel: {}` 三个字段**恒为空数组**，而前端 `用量分布` 默认读 `source.projects`、`消耗最高的调用` 读 `source.sessions`、趋势图模型筛选读 `dailyByModel`。另外 `models[]` 缺少前端构建下拉所需的 `key` 字段。
- **修复**：
  - `projects` 改为**按账号**聚合（网关没有「项目」概念）。
  - `sessions` 改为**单次调用**按 Token 降序（每条网关请求就是一次独立调用）。
  - `dailyByModel` 按模型×日期填充，趋势图模型筛选可用。
  - `models[]` 补 `key` 字段。
- **文案同步**（代理层等长字节替换，不动二进制）：`按项目→按账号`、`按项目汇总本地 Token 用量。→按账号汇总本地 Token 用量。`、`消耗最高的会话→消耗最高的调用`、`按本地聚合…→按单次调用…`。
- 云端流水合并时**补上 `accountId` / `accountName`**（原本被丢弃，导致全部落进「未归属账号」并占据 100%）；未归因的历史记录改标为 `未归因（升级前记录）`。

#### 5. 主页文案按容器真实能力重写
- 原：`统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号、积分和签到状态。`（容器里 IDE / CLI 能力已物理移除，措辞失真）
- 新：`统一管理 WorkBuddy、账号池与 OpenAI 兼容网关服务、积分和签到状态。`

#### 6. 缓存与链路清晰度
- **强制 `cache-control: no-store`**：`assets/*.js` 文件名带 hash 不随改动变化，浏览器可能一直用旧副本，表现为「改了却没生效」。现在 HTML / JS 响应禁用缓存。
- **README 增加「三层账号调度链路」表**：明确账号池（决定 API 用哪个账号）、网关健康巡检、官方 CLI 自动轮换各自的作用范围与间隔，说明三者互补而非冲突。

---

## [v0.3.7] - 2026-09-18

### 📊 并发分摊可观测性：实证「1 国内 + 4 国际」确实并行调用
- **补齐网关侧归因能力**。此前网关只有 `lastSelectedAccountId` 这**一个全局值**，只能回答「最近一次是谁」，无法回答「并发时是不是真的分摊」。上游用量接口 `/billing/meter/get-user-request-usage` 对全部 5 个账号均返回 `404`，也走不通按账号归因。
- **新增选账号流水**：`_remember_selection()` 现在把每次选账号写入 `_SELECTION_LOG`（环形，保留最近 200 条），同时 `print` 一行 `[pool] <source> -> <name> (<id>)`，可直接 `docker logs` 核对。
- **新增接口**：
  - `GET /account-pool/selections?limit=N` —— 返回 `total` / `distinctAccounts` / `counts`（各账号命中次数）/ `recent`（最近明细）。
  - `POST /account-pool/selections/reset` —— 清空流水，便于做干净的压测观测。
  - `GET /account-pool/status` 增加 `selectionCounts` 字段。
- **修掉一个并发隐患**：`next_index` 的「读-改-写」不是原子操作，若选账号逻辑被放进线程池执行，两个并发请求会读到同一 index 而双双落到同一账号（表现为「并发时其实只用了其中一个」）。现在用 `_SELECTION_LOCK` 把「取号 + 递增」串起来。
- **WebUI**：账号卡片控制条新增 `已调用 N 次` 计数徽章（有调用时高亮）与 `清零统计` 按钮，自动分配提示改为「并发请求会分摊到 N 个已启用账号」；新增 `/api/account-pool/selections`、`/api/account-pool/selections/reset` 两条代理路由。
- **实测结论（20 并发，model=hy3）**：`HTTP 200 × 20`，20 个并发请求**均匀分摊到全部 5 个账号，各 4 次**；并发墙钟 3.91s vs 单请求均值 3.33s，确为并发而非串行。`docker logs` 中的 `[pool] auto` 序列为严格轮询的 4 轮完整覆盖。

---

## [v0.3.6] - 2026-09-18

### 🎯 补上真正的那两个按钮：卡片头部图标行的 IDE / CLI 槽位
- **v0.3.4 只移除了「底部 footer」那一份，漏掉了卡片头部账号名右侧的图标版**。官方 UI 里这组产品槽位存在两份：
  1. **底部 footer**（带文字标签）—— v0.3.4 已移除；
  2. **头部图标行** `div.ml-auto.flex.shrink-0.items-center.gap-1`（紧凑图标）—— **本次移除**。这才是用户截图上「账号后面一个 CodeBuddy IDE 和 CodeBuddy CLI 按钮」的位置。
- 头部图标行内含三个槽位，各自都有「已接入显示徽章 / 未接入显示按钮」两个分支，共六个 JSX 表达式：
  - `设为 WorkBuddy 当前账号` 按钮 与 `WorkBuddy 当前账号` 徽章
  - `切换到 CodeBuddy IDE` 按钮 与 `CodeBuddy IDE 当前账号` 徽章
  - `设为 CodeBuddy CLI 当前账号` 按钮 与 `CodeBuddy CLI 当前账号` 徽章
  整行 `div` 一次性物理移除（2790 字节），父节点 `children` 第三项变为 `null`，React 渲染正常。
- **为什么上一版没发现**：v0.3.4 的字节断言用的是固定字面量 `aria-label":"设为 CodeBuddy CLI 当前账号"`，而这些 aria-label 实际由三元表达式动态拼接（`b?"正在切换 CodeBuddy CLI 当前账号":"设为 CodeBuddy CLI 当前账号"`），字面量在原始 bundle 里出现次数本来就是 0，断言等于没验。本次改为**用真实浏览器加载页面后查 DOM**，并断言 `[aria-label*="当前账号"]` 数量为 0。
- 保留项：`管理账号`（更多账号操作）下拉里的「刷新 Token / 手动签到 / 删除账号」在容器内可用，未做改动。

---

## [v0.3.5] - 2026-09-18

### 🔍 修复「最近使用账号」记录缺失，并在卡片上标出最近调用
- **问题**：`select_account()` 只在 auto 轮询分支写入 `last_selected_id`，走「请求指定账号」与「manual 固定账号」两条路径时不落记录。结果是 `/account-pool/status` 的 `lastSelectedAccountId` 会停留在上一次自动分配的结果，看起来像「手动设置没生效」——实测中手动固定与 header 指定都会显示成同一个陈旧账号。
- **修复**：新增 `_remember_selection(acc, source)`，三条路径（`request` / `manual` / `auto`）统一记录账号与来源；状态接口新增 `lastSelectedSource` 字段，可直接看出本次选择是来自请求指定、手动固定还是自动轮询。
- **WebUI**：账号卡片控制条上为「最近一次真实调用所用账号」增加 `最近调用` 虚线标签，便于直观确认账号池是否按预期分配。
- **验证**：容器内真实调用验证 —— manual 固定稳定命中指定账号；`X-WorkBuddy-Account-Id` 单次覆盖生效；已停用账号被指定时返回 `409`；auto 模式连续 5 次调用分摊到 5 个不同账号。

---

## [v0.3.4] - 2026-09-18

### 🧩 账号调用按钮物理切除 + 账号池并行 / 手动调度
- **账号卡片三个产品槽位彻底物理移除**：
  - WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 三个「设为当前账号 / 当前账号」槽位位于同一个 `m.jsxs("footer", ...)` 容器内，现已整块从二进制中物理删除。
  - 其他页面复用的 `Kb`「当前账号」徽章组件同步清空返回值（`_nullify_function_return`，保留函数声明结构）。
  - 不再依赖 CSS 属性选择器或 `MutationObserver` 遮掩——此前两次遮掩方案在 React 异步渲染时机下均被证实不可靠。
- **补丁引擎升级为「锚点 + 括号配平扫描」**：
  - `patch_binary.py` 弃用硬编码超长字节串，改为 `_scan_expr()` 括号配平扫描。
  - 扫描器正确跳过字符串与模板字面量（含 `` `${}` `` 递归），因此 `className` 中的 `[ ] ( )` 不再干扰表达式边界判定。
  - 所有替换仍严格等长，二进制偏移表不受影响（实测 1059826 → 1059826 字节）。
- **清理配套遮掩代码**：`web_proxy.py` 删除 `WB_UNSUPPORTED_LABELS` / `WB_UNSUPPORTED_STATUS` 常量、`aria-label` 匹配逻辑、右上角 `statusIcons` 遮掩块及对应 CSS，避免「二进制已移除 + 前端还在遮掩」的双份维护。
- **账号池：请求级并行调用**：
  - 新增 `account_pool_config.json` 与 `select_account()`。一条对话请求仍由单个账号完成（避免上下文与计费错乱），但**多个并发请求会分摊到不同账号**，不再全部挤在 `activeAccountId`。
  - 账号池候选先按 `enabledAccountIds` 过滤，再剔除 token 缺失或已过期的账号。
- **启用开关 + 手动首选**：
  - 每个账号卡片新增 `[参与调用 · 点击停用]` 与 `[设为首选]` 两个按钮（由 `web_proxy.py` 注入，走控制台 `/api/account-pool`）。
  - `enabledAccountIds` 为空数组时语义为「全部启用」；保存为明确列表后即成为白名单。
  - `mode=manual` 时固定使用 `manualAccountId`；单次请求也可用 `X-WorkBuddy-Account-Id` 请求头或 `body.account_id` 临时指定账号。
- **新增账号池接口**：网关 `GET /account-pool/status`、`PUT /account-pool/config`；控制台 `GET/PUT /api/account-pool` 转发。

---

## [v0.3.3] - 2026-09-18

### 🔧 容器界面残留再清理 + 请求明细排序与精简
- **彻底隐藏 CodeBuddy IDE / CLI「当前账号」状态徽章**：这些绿色对勾徽章检测的是宿主机桌面客户端，容器里恒为「未运行 / 未安装」。从 CSS 选择器层面直接命中（`[role="status"][aria-label="..."]`），无须依赖 JS 时机。
- **请求明细按时间倒序展示**：`token_tracker.py` 改为显式按 `timestamp` 倒序，不再依赖上游流水的拼接顺序。
- **明细精简为最近 200 条**：避免返回体过大；前端默认每页 50 条保持不变。

---

## [v0.3.2] - 2026-09-18

### 🧾 账号卡片模型清单与容器界面二次提纯
- **账号卡片动态模型清单重写**：
  - 可用清单改为直接读取网关 `/v1/models`（自动发现，非硬编码），与全局 32 款模型完全一致，不再出现「全局 32、卡片 23」的口径差异。
  - 新增「已调用」高亮：真实产生过调用的模型单独着色，悬停显示调用次数与消耗积分。
  - 新添加、尚无调用记录的账号同样展示完整可用清单，并标注「暂无调用记录」，不再留空。
- **移除无效的桌面程序状态图标**：
  - 右上角 WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 三个状态图标检测的是宿主机桌面客户端，容器内恒为「未运行 / 未安装」，悬停提示纯属误导，已整组移除。
- **模型获取链路修复**：
  - 修正 GitHub Actions 中残留的 `type=raw,value=v0.1.0` 标签规则（此前每次构建都会覆盖 v0.1.0 标签）。
  - 清理仓库中未被使用的重复 `main.py` / `web_proxy.py` / `token_tracker.py`（镜像实际只打包 `gateway/`），避免误改无效文件。
- **文档与日志对齐**：补全 v0.3.1 缺失的更新日志，README 补齐自动发现的 7 款模型与网关内置接口说明。

---

## [v0.3.1] - 2026-09-18

### 📋 请求明细平铺与容器交互提纯
- **「请求明细」从 Modal 弹窗重构为原生平铺展示**：打开 Token 统计页面后直接在底部平铺呈现最近调用的模型、时间、会话标识、所属网关与用量构成比，保留 50 条/页分页与悬停 Tooltip。
- **清除冗余操作**：移除右上角多余的「查看请求明细」弹窗按钮，整页排版自然连贯。
- **二进制补丁完善**：持续遵循等长无损字节补丁标准，保障底层 Axum 与前端 React 运行时稳定。

---

## [v0.3.0] - 2026-09-18

### 🌟 容器环境深度提纯与交互重构
- **源码级物理切除无效功能**：
  - 彻底剔除「导入本机账号」按钮，不再产生任何无用 DOM 及控制台 400 报错。
  - 彻底剔除「Token 统计」上方 4 个针对桌面客户端的分类按钮（WorkBuddy / WorkBuddy 国际版 / CodeBuddy CLI / CodeBuddy IDE），直接聚合呈现全量 Token 数据。
  - 剔除 Token 统计中冗余的「Token 总览」多余提示标题，页面视觉更加紧凑专业。
  - 彻底移除设置页面中无效的「权限检测」模块，优化设置副标题为「自动签到、账号保活与接口轮换配置。」
- **Token 统计全链路数据引擎上线**：
  - 新增 `token_tracker.py`，网关层（18091 端口）实时精准统计所有请求的 Prompt / Completion Tokens、响应耗时与模型归类。
  - 自动桥接云端真实历史流水，告别空白页面，提供按日堆叠趋势、模型用量分布柱状图与明细列表。
- **构建补丁工程化**：
  - 完善 `patch_binary.py`，所有切除与替换逻辑均基于严格等长字节对齐，保障核心二进制 0 语法损伤。

---

## [v0.2.0] - 2026-09-18

### 🚀 体验升级与全量顶级模型网关支持
- **可折叠收纳侧边栏**：新增侧边栏折叠按钮，支持 220px / 68px 动效切换，并支持 `localStorage` 记忆状态。
- **SPA 路由与设置页彻底修复**：修正 SPA 路由返回 `application/octet-stream` 导致的异常白屏与误下载。
- **Mac 桌面残留过滤**：消除「在 Finder 中显示」、「打开完全磁盘访问」等桌面弹窗提示。
- **全量前沿模型端到端解锁**：
  - 实测接入 **25+ 款主流大模型与官方工作模式**：涵盖混元 Hy3（思考模型）、DeepSeek-V3、OpenAI GPT-5.6/5.5/5.4/5.3、Gemini-3.5/3.1、GLM-5.3/5.2、Kimi-K3、MiniMax-M3。
  - 支持双向流式/非流式智能转换。
- **CodeBuddy CLI 自动引导**：容器启动全自动注入 Helper 与认证配置，消除「未接入 CLI」报警。

---

## [v0.1.2] - 2026-09-18
- **图标全链路对齐**：侧边栏、网页 Header 及 Favicon 统一映射 Unraid 高清官方圆角图标。
- **Sub2API 深度对接**：规范 OpenAI 格式渠道，支持全量模型映射。

---

## [v0.1.0] - 2026-09-18
- **初始版本发布**：实现 WorkBuddy / CodeBuddy 容器化基础运行环境与 OpenAI API 基础转换网关。
