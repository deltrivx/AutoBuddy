> v0.3.11 上线后做了一次全量验收（容器 / 二进制 / DOM / 接口 / 日志 / 数据 六个维度）。
> **主体全部达标**，但二进制全量扫描又揪出两个漏网项——它们都藏在「被删掉的 JS 逻辑原本会遮住」的位置。

## 1. 账号页「CodeBuddy CLI 接入」引导横幅（真实残留）

这是本次验收唯一的实质问题。

它的结构是：

```js
cond && p.jsxs(Bt,{className:"mb-4",children:[图标, 标题, 正文, 按钮]})
// cond = ie&&(!ie.configured||!mi&&!ie.helperSupportsAccountIds||ie.migrationRequired||ie.syncPending)
```

关键点在于：**它显示与否取决于官方后端返回的状态**。v0.3.11 只删掉了「切换 CLI 账号」等按钮，
横幅本身没进移除清单。当前后端恰好返回 `configured=true`，`cond` 为假，所以**它没显示**——
但只要后端改口（未接入 / 需升级 / 待同步），横幅就会带着
「接入 CLI / 更新 CLI 认证 / 升级 CLI helper」按钮重新冒出来。

这正是「彻底移除，不是隐藏」没有做到的最后一处。**本版把它从二进制里物理删掉**：

- 新增第 10 条补丁，锚点用带引号的完整标题 `"CodeBuddy CLI 接入"`
  （另外两处同名串出现在 toast 文案 `"CodeBuddy CLI 接入已更新"` / `"…失败"` 里，
  后接「已」/「失」而非引号，不会误匹配）；
- 回溯两层，把整个 `p.jsxs(Bt,{className:"mb-4",…})` 抹成 `null`（1570 字节）。

顺带说明：账号页右上角那组桌面状态图标（WorkBuddy / IDE / CLI）**及其悬停提示**
（`CodeBuddy CLI：已接入 · 当前账号：xxx`）已被 v0.3.11 的第 5 条补丁整块覆盖，属死代码，无需再动。

## 2. `/api/token-stats` 里两个死重的数据来源桶

`gateway/token_tracker.py` 原本返回 4 个 source 桶：`workbuddy` / `workbuddy-ai` /
`codebuddy-cli` / `codebuddy-ide`。后两者对应已移除的桌面 CLI / IDE，容器里不存在本地会话日志，
是纯死重。本版只保留 `workbuddy` 与 `workbuddy-ai` 两条真实产品线。

**安全性已核对**：官方前端取用逻辑是

```js
w = x.includes(n) ? n : x.includes("workbuddy") ? "workbuddy" : x[0]
y = e?.sources.find(s => s.source === n)
```

即使 localStorage 里残留 `codebuddy-cli`，也会自动回落到 `workbuddy`，不会白屏。

## 3. 验收结论（未改动项，留档）

- 补丁**这次真的生效了**：容器内二进制 md5 `6622cd46…` ≠ 官方 pristine `8e1dd723…`，字节数 12861680 不变。
- 设置页只剩 `settings-appearance` + `settings-auto-checkin`。
- 账号池 `.wb-pool-bar` 注入正常（国内版 3 个 / 国际版 4 个），
  说明此前做的**账号池、请求级归因、Token 契约**等优化一项没丢。
- 首页副标题由响应层等长替换（`web_proxy.py` 内带 `len(old)==len(new)` 断言，不等长直接抛错，不会静默失败）。
- 接口全 200；`/api/cli-info` 已不存在；`/api/account-pool` 无 CLI 字段；日志异常计数 0。

## 4. 遗留（本版未处理，待你决定）

- `/data/.codebuddy/`（**18 MB**，v0.3.9 时代容器内跑 CLI 留下的运行时数据：
  `traces/`、`projects/workspace`、`sessions/`、`plugins/`、`settings.json`）与
  `/data/.codebuddy-rotate/`（`helper.cjs` + `state.json`）。
  这两个目录是 CLI 时代的持久化残留，删掉即可回收空间，不影响现有功能。
- `.wb-switch/auto_travel_config.json`（`enabled: true`）是**官方后端**的「自动出行」功能
  （`/api/travel/status`、`/api/travel/config`），与「自动签到」同属积分保活家族，本版未动。
  如需一并移除请告知——注意它目前是开启状态，关掉可能减少积分收益。
- **安全待办**：轮换 5 个账号的 access_token / refresh_token。

## 5. 升级须知

- 数据卷不变，仍然是 `/data`（`/mnt/user/appdata/workbuddy-switch`）。
- 直接重建容器拉取 `latest` 即可，无需改挂载。
