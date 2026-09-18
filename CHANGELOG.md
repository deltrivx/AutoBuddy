# 更新日志 (Changelog)

本项目严格遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 规范。

---

## [v0.3.17] - 2026-09-19

### 🧹 彻底清除 `/data` 下的 CodeBuddy CLI 残留

CodeBuddy CLI 早在 v0.3.11 就从镜像里移除了，但它在**数据卷**里留下的痕迹一直没清。
本版做一次性清理，并修掉唯一还在「制造」残留的源头。

**清除清单**（均在容器 `/data`，删除前已打包备份到
`/mnt/user/appdata/workbuddy-switch/backup/`）：

| 路径 | 体积 | 性质 |
| :--- | ---: | :--- |
| `/data/.codebuddy/` | 18 MB | CLI 家目录：`settings.json`（`apiKeyHelper` 指向 CLI helper）、插件市场缓存、`projects/` `sessions/` `traces/` `shell-snapshots/`、`local_storage/` |
| `/data/.codebuddy-rotate/` | 8 KB | CLI 的 `helper.cjs`（apiKeyHelper）+ `state.json` |
| `/data/.wb-switch/hook.sh` | 102 B | CLI 的 Stop / FinalStop 钩子脚本 |
| `/data/.wb-switch/hook-events.jsonl` | 865 B | 上述钩子的事件日志（`"client":"CLI"`） |
| `/data/.wb-switch/hook-backups/` | 178 B | CLI `settings.json` 的备份 |
| `/data/.npm/` | 2.8 MB | 当初 `npm i -g` 装 CLI 留下的 npm 缓存 |

**源头修复**（本版唯一代码改动，`entrypoint.sh`）：

```diff
- mkdir -p /data/.wb-switch/rotate
+ # 只确保数据目录存在。不要再创建 /data/.wb-switch/rotate —— 那是 CodeBuddy CLI
+ # 时代的 token 轮换目录，CLI 已于 v0.3.11 彻底移除，空目录留着纯属残留。
+ mkdir -p /data/.wb-switch
```

`/data/.wb-switch/rotate` 是个**每次容器启动都会被重建**的空目录 —— 光删文件治不了，
必须改 entrypoint。

**明确不动**的两项（经核实不属于 CLI 残留）：

- `/data/.config/CodeBuddy`、`/data/.config/CodeBuddy CN` —— 属**IDE** 集成，
  官方二进制至今仍在写 `/data/.wb-switch/codebuddy_ide.json`（实测持续更新）；
- `/data/.wb-switch/auto_rotate_config.json` / `auto_rotate_logs.json` ——
  官方二进制**自带的**账号轮换功能（Rust core `wb_switch_core::modules::rotate`，
  对应 `/api/rotate/config`），当前 `enabled: false`，与 CLI 无关。

**验收**：

- 删除后重启容器，上述 6 项**均未被重建**（`rotate` 需等本版镜像上线后才消失）；
- `/data` 只剩 `.config`（IDE）与 `.wb-switch`（实时数据）；
- 账号 7 个完好；`18090 /`、`/api/account-pool`、`/api/account-models`、`/api/token-stats`、
  `18091 /health`、`/v1/models` 全 200；日志 0 异常；`/v1/chat/completions` 冒烟正常；
- 响应层文案改写仍生效（`按项目`=0 / `按账号`>0）。

**顺带查明（未改动）**：官方前端里仍有一段 **CodeBuddy CLI 接入确认对话框**的代码
（含 `~/.codebuddy-rotate/helper.cjs` 文案）。经静态可达性分析确认它是**死代码** ——
唯一的开启函数 `async function $n(){Lt(!0)}` 在全 bundle 内**零调用点**，
另一处 `Ot` 对话框的唯一设置者 `An` 也只作为卡片 prop 传入、卡片内部从不调用它。
浏览器实测四个页面 DOM 中 `CLI` / `CodeBuddy` / `IDE` / `helper.cjs` 命中数**均为 0**。
因不可达、用户不可见，本版不做二进制补丁。

---

## [v0.3.16] - 2026-09-19

### 🎯 账号卡片：去掉模型维度的调用次数，只保留账号维度

**用户反馈**：卡片上同时存在两个「调用次数」，口径不同却极易混淆 ——

| 位置 | 内容 | 实际口径 |
| :--- | :--- | :--- |
| 模型标签 tooltip | `网关调用 8 次 · 约 88 Token · 官方记录 8 次 · 积分 0` | **模型**被调用几次 |
| 模型区标题 | `可用模型 · 33（已调用 5）` | 该账号**用过几个模型** |
| 账号池条 | `已调用 0 次` | 该**账号**被分摊到几次请求 |

「已调用 5」与「已调用 0 次」用同一个词、含义完全不同 —— 这正是混淆源。

**改法**（`web_proxy.py` 的 `injectAccountModels()`）：

- 模型标签 tooltip **去掉** `网关调用 N 次` 与 `官方记录 N 次`，只留用量类信息（`约 N Token` / `积分 N`）；
- 模型区标题**去掉** `（已调用 N）` / `（暂无调用记录）` 后缀，恒为 `可用模型 · 33`；
- 未命中 usage 明细但确实用过的标签，tooltip 改为 `该账号用过此模型`（点明是**账号**维度）；
- **保留**标签高亮 `wb-am-tag-used`（用户明确认可：颜色变化已足够表达"这个模型被调用过"）；
- **保留**账号池条的 `已调用 N 次` —— 卡片上唯一的调用次数，不再有二义。

`gatewayCalls` 字段仍在 `/api/account-models` 响应里（接口数据，未删，避免破坏外部消费方），
只是前端不再渲染它。

**验证**（浏览器实测，两个 Tab 共 7 张卡片）：

- 标题全部为 `可用模型 · 33`；33 个标签中带 `调用/官方记录` 字样的 **0** 个
- 高亮计数保持正确（国内版 5/1/1；国际版 4/6/4/24）
- 账号池条 `已调用 0 次` 保留

---

## [v0.3.15] - 2026-09-19

### 🩹 更正 v0.3.14 的判定 + 把响应层的死规则清掉

v0.3.14 把账号页副标题记作「用户可见的真问题」。**这个判定不成立**，本节更正，并把由此暴露的机制问题一并处理。

#### 1. 更正：那句文案用户从来没见过

`gateway/web_proxy.py` 的 `clean_mac_content()` 里**早就有一条针对它的等长替换**，从 v0.3.8 起就在跑：

```python
("统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号、积分和签到状态。",
 "统一管理 WorkBuddy、账号池与 OpenAI 兼容网关服务、积分和签到状态。"),
```

该函数对 `text/javascript` 响应同样生效（`elif "javascript" in media_type`），所以浏览器拿到的是**改写后**的文案。
v0.3.14 只看了二进制，没查响应层，把一个「源码级陈旧」误判成了「用户可见缺陷」。

**实测证据**（对线上 JS 资源 `/assets/index-CzM10CWE.js` 计数，判定响应层是否真在生效）：

| 串 | 计数 | 说明 |
| :--- | ---: | :--- |
| `按项目` / `按账号` | 0 / 5 | 同一份响应里的替换生效 → 机制确实在工作 |
| `消耗最高的会话` / `消耗最高的调用` | 0 / 1 | 同上 |
| 旧副标题 | 0 | 已被响应层改掉 |

**结论修正**：这不是用户可见 bug，而是**源码里残留着描述已移除产品的文案 + 一条掩盖它的运行时补丁**。

#### 2. 清理：删掉那条已经永远命不中的响应层规则

v0.3.14 把该文案改到了二进制源头，于是响应层那条的 `old` 串在资源里不再存在 —— **一条永远命不中的死规则**。已删除。

**顺带确立了选型原则**（写进 `web_proxy.py` 注释）：

> 源头能改的结构性文案**优先放二进制补丁**。因为 `_replace_padded` 要求锚点**恰好命中 1 次**，
> 失配会让 CI 直接失败；而响应层的 `content.replace` 命不中时**静默无操作** ——
> 正是 v0.3.10「补丁全失效但构建 success」那类事故的同一形状。

#### 3. 加固：响应层替换加「零命中告警」（并修掉它自己的误报）

`TEXT_REPLACEMENTS` 循环现在统计命中数，为 0 时打一行：

```
[webui] 文案替换未命中（上游文案可能已变，请复核）: <old>
```

不抛异常（避免请求 500），但 `docker logs | grep 文案替换未命中` 就能发现上游改文案导致的静默失效。

**初版这里自己就踩了一个坑**：`clean_mac_content()` 对 HTML 也调用，而这几条是**前端 UI 文案、只存在于 JS 资源里**，
HTML 外壳（几百字节）里根本没有它们 —— 于是**每个 HTML 请求都误报 3 条**。修正：

- 加 `is_js` 形参，`TEXT_REPLACEMENTS` 只在 `elif "javascript" in media_type` 分支（`is_js=True`）里跑；
- 命中数告警**每个串只打一次**（模块级 `_MISS_WARNED`），避免刷屏。

单测覆盖四个分支：HTML 分支 0 告警 / JS 全命中 0 告警 / JS 未命中告警 3 条 / 重复未命中不再告警。

#### 4. 顺带审计：端口清理规则命中 0 属预期

`replacements` 里那 4 条 `:57890` 端口改写，实测在后端原始响应（HTML 489 B / JS 1066264 B）里命中 **0** ——
二进制补丁把唯一的 `const X="http://127.0.0.1:57890";` 换成 `window.location.origin` 之后就没事可做了。
它们保留作**兜底网**（上游若在别处新写硬编码后端地址仍能掰回 18090），已在代码注释里写明「命中 0 属预期，不是失效」。

#### 5. 校验

- 二进制补丁仍为 **19 条**，`applied: 19 failed: 0`（本版未改补丁）
- `web_proxy.py` 语法检查通过 + 4 分支单测通过
- 容器内热推送实测：HTML/JS 均 200，3 条替换全部命中，`docker logs` 里告警/异常 **0** 条
- 浏览器复验：账号页副标题 = `统一管理 WorkBuddy 账号、积分与签到状态，并为 API 网关提供账号。`

---

## [v0.3.14] - 2026-09-19

### 🔎 二轮验收：账号页副标题在**二进制里**仍宣称「管理 CodeBuddy IDE 与 CodeBuddy CLI 账号」

> ⚠️ **本节初版把此判定写成「用户可见的真问题」，是错的。** 详见 v0.3.15 的更正：
> 响应层自 v0.3.8 起就在改这句文案，用户看到的一直是改写后的版本。
> 真实性质是**源码级残留**（二进制里留着陈旧文案 + 一条运行时补丁掩盖它），修法是把它挪到源头。

v0.3.13 的普查只筛了「桌面 / CLI 关键词」，漏掉了**把已移除产品当作功能卖点写进正常句子**的文案。
本轮换成更严的筛法：把 patched 二进制前端 JS 区段里**所有**中文界面串抽出来（173 条），
再对账号页 `kZ`、账号卡片 `$X`、设置页、导航逐个组件全量过一遍，而不是只匹配关键词。

#### 1. 账号管理页副标题（二进制里的陈旧文案）

```jsx
<p className="mt-2 text-sm leading-6 text-muted-foreground">
  统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号、积分和签到状态。
</p>
```

它把 `CodeBuddy IDE` / `CodeBuddy CLI` 说成本页的管理对象，而这两个桌面端产品早在 v0.3.11 就被物理移除。
（这句在浏览器里会被响应层改写，见 v0.3.15；但二进制源头里确实留着这段陈述不存在的功能的文案。）

- 新文案：**「统一管理 WorkBuddy 账号、积分与签到状态，并为 API 网关提供账号。」**（90 → 88 字节，补 2 空格）

#### 2. 顺带把三处「疑似残留」查实为死代码（不动）

| 文案 | 宿主 | 判定依据 |
| :--- | :--- | :--- |
| 复制会话到目标账号 / 如何授权（只需 3 步）/ 在 Finder 中显示 / 立即检测 | `AZ` 弹窗 | 组件**有**调用点（`p.jsx(AZ,{open:C!==null,…})`），但 `[C,N]=useState(null)` 而全文件 `N(` **只被调用一次且是 `N(null)`** → `open` 恒为 false |
| 切换 CodeBuddy CLI / 关闭 CLI 并切换 | `Ka` 弹窗 | `[Ot,Gn]=useState(null)`；`Ot` 只由 `An` 写入，而 `An` 仅作为 `onSwitchCodebuddyCli` prop 传给卡片，卡片上那个按钮已抹除 → 无调用者 |
| 将注入凭证并重启 CodeBuddy IDE / CodeBuddy IDE 已切换 | `pi` | 仅作为 `onSwitchCodebuddyCnIde` prop 传递，无调用者 |

> 上一版对 `AZ` 的结论是「无任何调用点」，**不准确**：它确实被渲染，死因是状态永远不为真。
> 结论相同但依据要修正——这类「有调用点但状态恒假」的弹窗，必须查到 setter 的**写入值**，不能只看调用点数量。

#### 3. 「模型限额」徽章：确认容器内不可达

账号卡片 `$X` 仍接收 `rateLimits` prop，卡片上也有 `DX()` 渲染的 `模型限额：…` 徽章（`variant:"warning"`），
一度怀疑「限额监听」还有可见残留。查实：

```js
async function ds(le){ if(J!==!0)return; … const ze=await LF();
  const bt={}; for(const tt of ze.accounts??[]) tt.limited?.length&&(bt[tt.accountId]=tt.limited); q(bt) }
```

轮询确实在跑（`J` 由 `/api/rate-limits/config` 的 `enabled:true` 置真），但 `LF()` 即 `/api/rate-limits`
在容器里恒返回 `{"accounts":[]}`（它扫的是**宿主机 IDE 日志**，容器内没有），映射表恒为空 →
`DX(undefined,t)` 走 `n.length===0` 直接 `return null`。**徽章不可能渲染**，属死代码。

#### 4. 复核其余界面（全部干净）

- 导航 4 项：账号管理 / Token 统计 / 积分统计 / 设置 ✅
- 设置页仅剩 2 个 section：`settings-auto-checkin`（自动签到）、`settings-appearance`（外观）✅
- 账号卡片可见文案：签到状态、积分包、`建议优先使用`、`N 个工具正在使用` —— 均为活功能 ✅
- `未登录` / `尚未接入` / `未检测到` / `未安装` 只是被已抹除的桌面状态区使用的局部变量 ✅
- 容器挂载只剩 `/data`（`/workspace` 已在早前移除）✅

#### 5. 校验

- 补丁数 18 → **19**，pristine 上 `applied: 19 failed: 0`
- 新增本地基线复现：用补丁脚本对 pristine 跑一遍，产物 md5 与线上部署二进制**逐字节一致**
  （`2d46309d2a6dcefcadc2f8291d41e950`），证明本地验证环境可信
- 新旧产物 diff 仅副标题那 90 字节区间（67 字节实际变化），无副作用
- 总校验 `FAILED: 0`

---

## [v0.3.13] - 2026-09-19

### 🔎 继续验收：账号列表空状态还在引导用户点已删除的按钮

v0.3.12 之后把「全量 UI 文案普查」做完了——做法是：补丁已把要移除的 JSX 整块换成 `null`，
所以**凡是在 patched 二进制里还能找到的中文界面文案，就一定不在已抹除区间内**。把这类文案全量抽出来
（`children` / `aria-label` / `placeholder` / `title` / `description`），再按桌面/CLI 关键词筛一遍，
剩下 7 处。逐个追查可达性后，只有一处是**真问题**。

#### 1. 账号列表空状态引用了已移除的按钮（唯一真问题）

```jsx
t === "ai" ? <>
  <p>暂无国际版账号。</p>
  <p>请确认本机已安装 {Ct}（客户端下载域名 {sF(t)}）并登录，
     再点击上方「导入本机国际版账号」；也可以直接「OAuth 登录」添加国际版账号。</p>
</> : "暂无账号。点击上方按钮导入本机账号或扫码登录。"
```

它在账号列表为空时渲染，却让用户去点**已经被删掉的**「导入本机国际版账号」/「导入本机账号」按钮，
而且「请确认本机已安装…」本身就是桌面端话术。属于指向不存在入口的误导文案。

- 国际版那段含 `{Ct}` / `{sF(t)}` 两处 JSX 插值，正则跨不了嵌套，所以新增了一个工具函数
  **`_replace_call()`**：用 `_call_start_before` + `_scan_expr` 做括号配平定位，把整个 `<p>` 换掉。
- 国内版是纯字符串，直接等长改写。
- 新文案：国际版 →「国际版账号请用上方「OAuth 扫码添加」或「导入备份」添加。」；
  国内版 →「暂无账号。请用上方「OAuth 扫码添加」添加。」（都是容器里真实存在的入口）。

#### 2. 另外 6 处确认是死代码，不动

逐个查「打开入口」是否已被抹除，结论如下：

| 文案 | 宿主 | 结论 |
| :--- | :--- | :--- |
| `关闭 CLI 并切换` / `切换 CodeBuddy CLI` | 账号卡片里的 CLI 切换弹窗（`open:Ot!==null`） | 唯一入口是已被抹除的产品槽位按钮 → 不可达 |
| `将注入凭证并重启 CodeBuddy IDE` | IDE 切换 toast（`pi()`） | 同上 → 不可达 |
| `点击下方「打开完全磁盘访问」` | 权限引导弹窗 `AZ({open,...})` | **没有任何调用点**（唯一命中是定义本身）→ 不可达 |
| `在 Finder 中显示` | 导出弹窗的 `aZ()` / `iZ()` | 只在桌面分支设置 `v` 后才渲染；浏览器分支（`fn()` 为真）下载完直接关弹窗 → 不可达；且响应层已把它改写成「在文件管理器中显示」 |
| `客户端` | Rust 后端字符串表 / Token 表格列头 | 非前端残留 |

> 顺带确认了一个此前没查过的点：`fn()` 是**浏览器检测**
> （`return typeof window<"u" && !("__TAURI_INTERNALS__"in window)`，为真 = 非 Tauri）。
> 全量核对 5 处 Tauri 动态 import，只有 `aZ`（reveal in dir）没加 `fn()` 守卫，
> 但它的渲染条件只由桌面分支设置，容器里不会触发。

补丁数 **16 → 18**，字节数 12861680 不变，括号配平与 pristine 一致。

---

## [v0.3.12] - 2026-09-18

### 🧹 验收补漏：把最后两个 CLI 残留从二进制里物理删掉

v0.3.11 上线后做了一次全量验收（容器 / 二进制 / DOM / 接口 / 日志 / 数据六个维度）。
主体全部达标，但**二进制全量扫描**又揪出两个漏网项——它们都藏在「被删掉的 JS 逻辑原本会遮住」的位置。

#### 1. 账号页「CodeBuddy CLI 接入」引导横幅（真实残留）

- 该横幅结构为 `cond && p.jsxs(Bt,{className:"mb-4",children:[图标, 标题, 正文, 按钮]})`，
  其中 `cond = ie&&(!ie.configured||!mi&&!ie.helperSupportsAccountIds||ie.migrationRequired||ie.syncPending)`。
- 关键点：**它的显示与否取决于官方后端返回的状态**。v0.3.11 只删掉了「切换 CLI 账号」等按钮，
  横幅本身没进移除清单；而当前后端恰好返回 `configured=true` 使 `cond` 为假，所以**它没显示**——
  但只要后端改口（未接入 / 需升级 / 待同步），横幅就会带着「接入 CLI / 更新 CLI 认证 / 升级 CLI helper」
  按钮重新冒出来。这正是用户要求的「彻底移除，不是隐藏」没有做到的最后一处。
- 处理：新增第 10 条补丁，用带引号的完整标题 `"CodeBuddy CLI 接入"` 作锚点（另外两处同名串出现在
  toast 文案 `"CodeBuddy CLI 接入已更新"` / `"…失败"` 里，后接「已」/「失」而非引号，不会误匹配），
  回溯两层把整个 `p.jsxs(Bt,{className:"mb-4",…})` 抹成 `null`（1570 字节）。
- 顺带说明：账号页右上角那组桌面状态图标（WorkBuddy / IDE / CLI）**及其悬停提示**
  （`CodeBuddy CLI：已接入 · 当前账号：xxx`）已被 v0.3.11 的第 5 条补丁整块覆盖，属于死代码，无需再动。

#### 2. `/api/token-stats` 里两个死重的数据来源桶

- `gateway/token_tracker.py` 原本返回 4 个 source 桶：`workbuddy` / `workbuddy-ai` / `codebuddy-cli` /
  `codebuddy-ide`。后两者对应已移除的桌面 CLI / IDE，容器里不存在本地会话日志，是纯死重。
- 处理：只保留 `workbuddy` 与 `workbuddy-ai` 两条真实产品线。
- **安全性已核对**：官方前端取用逻辑是
  `w = x.includes(n) ? n : x.includes("workbuddy") ? "workbuddy" : x[0]`，再 `sources.find(s=>s.source===n)`。
  即使 localStorage 里残留 `codebuddy-cli`，也会自动回落到 `workbuddy`，不会白屏。

#### 3. 验收结论（未改动项，仅供留档）

- 补丁**这次真的生效了**：容器内二进制 md5 `6622cd46…` ≠ 官方 pristine `8e1dd723…`，字节数 12861680 不变。
- 设置页只剩 `settings-appearance` + `settings-auto-checkin`；账号池 `.wb-pool-bar` 注入正常（3 / 4 个），
  说明此前做的账号池、归因、Token 契约等优化**一项没丢**。
- 首页副标题由响应层等长替换（`web_proxy.py` 里带 `len(old)==len(new)` 断言，不等长直接抛错，不会静默失败）。
- 遗留（未处理，待用户决定）：`/data/.codebuddy/`（18 MB，v0.3.9 CLI 运行时数据）与
  `/data/.codebuddy-rotate/`（helper.cjs + state.json）是 CLI 时代留下的持久化残留，删掉即可回收。

---

## [v0.3.11] - 2026-09-18

### 🔍 回归修复 + 范围收窄：彻底移除 CLI，功能只留「自动签到」与「提供 API」

本版起于一个线上现象：**每个账号前面又出现了三个桌面图标**。排查后发现问题比表象严重得多。

#### 1. 根因：补丁整体失效，且失败被静默吞掉

- 官方 `wb-switch` 已从 `0.1.36` 升到 **`0.1.40`**，压缩产物里的 JSX 变量名从 **`m.jsx` / `m.jsxs` 变成了 `p.jsx` / `p.jsxs`**（后端端点常量也从 `Db` 变成 `zb`）。
- 而 `patch/patch_binary.py` 的 13 条锚点**全部把 `m.jsx` 写死**，于是**每一条都失配**。
- 更糟的是：锚点失配时脚本只 `print("- skip ...")` 就继续，Dockerfile 又用
  `python3 patch_binary.py ... 2>/dev/null || true` 吞掉失败——**补丁全失效，镜像构建照样 success**。
- 实证：线上容器内 `/usr/lib/node_modules/workbuddy-switch/bin/wb-switch-linux-x64` 的 md5
  与官方 `0.1.40` 原始二进制**完全相同**（`8e1dd723ca74e2a8405bc8be191abec5`），即 v0.3.10 的补丁**一个字节都没改**。
- 所以三个桌面图标、footer、导入本机账号、权限检测、桌面状态图标**全部复活**。

#### 2. 重写 `patch/patch_binary.py`：锚点自适应 + 失配即失败

- **锚点不再依赖压缩变量名**：一律用稳定的**文案 / `className` / `id`** 定位，再回溯到 JSX 调用起点；变量名从匹配结果里现取。新增
  `_call_start_before()` 做**真正的包含性判断**（marker 常落在 `children:[a?X.jsx(..):Y.jsx(..),…]` 三元里，
  简单取「最近的 `.jsx(` 匹配」会命中兄弟调用而不是包裹调用）。
- **锚点失配 = 构建失败**：脚本收集所有失败项，末尾以**非零码退出**；Dockerfile 用 `set -eux` 且**去掉了 `|| true` 与 `2>/dev/null`**。
- **锁定上游版本**：`npm i -g workbuddy-switch@latest` → `ARG WB_SWITCH_VERSION=0.1.40`，避免上游发版静默改变结构。

#### 3. 移除清单扩充（0.1.40 结构下重新逐条校准）

| 目标 | 0.1.40 中的锚点 | 结果 |
| :--- | :--- | :--- |
| 后端端点 | `const zb="http://127.0.0.1:57890";` | → `window.location.origin` |
| 账号卡片头部三个图标 | `className:"ml-auto flex shrink-0 items-center gap-1"` | 整块 → `null` |
| 账号卡片底部三个产品槽位 | `className:"flex flex-wrap items-center gap-2.5 border-t px-5 py-2.5"` | 整块 → `null` |
| 「当前账号」徽章组件 | `function Qb({product:` | 返回值 → `null` |
| 桌面程序运行状态图标组 | `className:"flex shrink-0 items-center gap-4 pt-1"` | 整块 → `null` |
| 导入本机账号按钮 | `"导入本机国际版账号":"导入本机账号"`（回溯 2 层到 Tooltip 包裹） | 整块 → `null` |
| Token 统计页数据来源 Tab | `className:"mb-8 min-w-0 gap-0"` | 整块 → `null` |
| 「Token 总览」冗余标题 | `id:"token-overview-title"` | 整块 → `null` |
| CLI 专属「查看请求明细」按钮 | `n==="codebuddy-cli"&&` | 短路表达式 → `null` |
| **设置页 5 个桌面向 section** | `id:"settings-auto-rotate"` / `-permission` / `-rate-limit` / `-startup` / `-updates` | 组件返回值 → `null` |

- 新增的 5 个 section 移除，对应「其他边缘功能不需要」：
  - **权限检测**：检测 macOS 完全磁盘访问，容器里不存在该能力。
  - **启动设置**：`开机时静默启动到托盘`，纯桌面登录项 / 托盘概念。
  - **自动更新**：容器里升级靠镜像重建，自更新只会写进易失层。
  - **限额监听**：两个开关分别依赖「扫描 CodeBuddy IDE 日志」与「向桌面客户端装 hook」，容器里两者都不存在。
  - **CodeBuddy CLI 自动轮换**：随 CLI 一并移除。
- **保留** `外观`（浅色/深色主题，WebUI 里真实可用）与 `自动签到`（核心功能）。
- 设置页总描述文案 `自动签到、限额监听、权限检测与自动更新配置。` → `自动签到与账号保活，对外提供 OpenAI 兼容接口。`（等长替换，右侧补空格）。

#### 4. 彻底移除 CodeBuddy CLI（不是隐藏）

- **Dockerfile**：删除 `npm i -g @tencent-ai/codebuddy-code`、`ENV DISABLE_AUTOUPDATER=1`、
  `mkdir -p /workspace`、system 级 git 身份，以及随之而来的 `git` 依赖。
- **删除 `gateway/cli_bootstrap.py`**（v0.3.9 新增的 CLI 绑定引导）与 `entrypoint.sh` 里的步骤 2 / 2b。
- **`gateway/web_proxy.py`**：删除 `GET /api/cli-info` 接口、`.wb-cli-scope-note` / `.wb-cli-status*` /
  `.wb-pool-bound` 三组样式、`injectCliScopeNote()`、`官方 CLI 绑定` 标记，以及 `/api/account-pool` 里
  并入的 `cliActiveAccountId` / `cliActiveAccountName` / `cliConfigured`；随之不再需要的
  `shutil` / `subprocess` / `time` 三个 import 一并删除。
- **Unraid 模板**：删除 `/workspace` Path 项（备份 `.bak-v0310`），只保留 `18090` / `18091` / `/data`。
- 镜像体积随之回落（CLI 解压约 175 MB + git 及其依赖）。

#### 5. 文档

- README：删除「容器内使用 CodeBuddy CLI」整章与 `git 身份` / `/workspace` 相关小节；三层账号调度链路收敛为
  **两层**；移除 `/api/cli-info` 接口行与 `官方 CLI 绑定` 标记说明；新增「🔧 容器化补丁机制（维护须知）」
  章节，写明两条纪律与升级上游版本的验证流程。
- 明确项目定位：**只做账号自动签到保活 + 提供 OpenAI 兼容 API**，除 `/data` 外不需要其他挂载。

---

## [v0.3.10] - 2026-09-18

### 🧰 补全容器内 CLI 的运行条件（git 身份 + 工作目录）

v0.3.9 让 CodeBuddy CLI 在容器里能跑起来之后，环境自检又发现还差两个「真容器环境」的必要条件。

#### 1. git 身份默认值（否则 CLI 的 git commit 会失败）
- **问题**：容器内 `git config --global --list` 为空。CodeBuddy CLI 的版本控制能力（查看 diff、提交变更、创建分支）依赖 `user.name` / `user.email`，缺失时提交直接失败。
- **修复**：Dockerfile 设 **system 级**默认值 `user.name="CodeBuddy CLI (container)"` / `user.email="codebuddy@container.local"`。用 system 级（`/etc/gitconfig`）是为了让用户在容器内用 `git config --global` 就能覆盖，不必改镜像。

#### 2. `/workspace` 挂载位（Unraid 模板）
- 模板新增可选 Path：`/workspace` ← `/mnt/user/appdata/workbuddy-switch/workspace`（`Display="advanced"`、`Required="false"`），原模板已备份为 `.bak-v039`。
- 容器内 `/workspace` 在镜像构建时已预建；不挂载也能跑 CLI，只是没有可操作的项目文件。

#### 3. 自检确认（无需改动）
- CLI **自带 ripgrep**（`vendor/ripgrep/arm64-linux/rg` 等多平台），无需另装。
- `DISABLE_AUTOUPDATER=1` 已生效；Node v20.20.2 / npm 10.8.2 / git 2.43.0。
- 体积：CLI 174M + workbuddy-switch 25M。

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
