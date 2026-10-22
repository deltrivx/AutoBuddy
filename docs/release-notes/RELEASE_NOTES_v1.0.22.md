修 11128 渠道拦截不换号致客户端全 400、补官方桌面端完整请求特征、网关自检 NameError、搜索栏昼夜适配与右端对齐且只在账号页出现、修账号卡片被误灰化的前端时序。

## 一、上游 400 code=11128 不再 100% 透传给客户端

### 背景与问题

用户实况：Mac-mini 上 DeepSeek Harness（DSH）经网关调 WorkBuddy API，窗口内 4 次请求全部 400、0 次成功；而同一时间池内其他客户端（Unraid 本机 6472 次 / FnOS 11 次）几乎全绿。上游错误码为：

```
HTTP 400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel",
"displayTips":{"en":"This call failed channel verification, possibly because it
was not sent from an official client. Please resend from the official client."}}
```

### 根因

判定与记账两处口径不一致：

- `cooldown.py::_classify` 早已把 400 + 11128 归类为 `channel`，施加 30 分钟账号冷却，源码注释明确写着「实测 11128 只落在单个账号，同期其他账号同形态全成功 → 账号级拦截，换号即可」；
- 但转发路径的 `_looks_like_model_unavailable()` 只认 `11102` / `11103` 两个码，11128 不在可重试集合里，于是上游一次账号级拦截被 100% 透传给客户端。

叠加会话粘性的自锁循环，故障被放大成客户端窗口内全失败：

```
请求 → 选中被封账号 → 400/11128 → unbind
     → 同 session_key 下个请求又选回它（池小时概率极高）
     → 再 400 → 再 unbind → …
```

### 修复

1. **新增 `_looks_like_channel_blocked()`**（main.py）：400 + code=11128 + `unapproved` 字样，与 `cooldown.py::_classify` 的 `channel` 分支判定口径完全一致，消除「账本认为该冷却换号、转发却当不可重试」的矛盾。
2. **两个重试循环（流式 / 非流式）接入该判定**：渠道拦截走换号路径，上限 = 候选账号数 + 1，全部失败才透传真实错误。
3. **渠道拦截不写模型黑名单**：`_learn_model_unavailable()` 只服务「账号 × 模型」不支持的情形。渠道封锁是账号级问题，误写会让该账号名下所有模型被一并拉黑——那是比原故障更难发现的损坏。
4. **粘性不再登记冷却中的账号**：`select_account()` 收尾的 `session_sticky.bind()` 前增加 `cooldown.is_cooling()` 判断，正在冷却（渠道封锁 / 限流 / 熔断）的账号不进粘性表，从源头拆掉上面的自锁循环。

---

## 二、出站请求模拟官方桌面端完整特征（不只是 UA）

### 背景

上游 11128 的 displayTips 直说「这次调用没通过渠道校验，可能不是官方客户端发出的」。此前只对齐了 User-Agent——那只是削去冰山一角：官方桌面端的请求特征是「头族 + 请求体」一整套餐，缺任何一块都仍是脚本客户端形状。

### 特征来源（全部是仓库内已有的一手样本，非推测）

| 样本 | 位置 | 提供什么 |
| :--- | :--- | :--- |
| `report_desktop_events` | `vendor/workbuddy_daily.py:3122` | 官方桌面域 `/v2/report` 的真实头族 |
| `t_daily_activity` | `vendor/workbuddy_daily.py:1207` | CLI 域 chat 的真实头族（`X-Domain` / `X-User-Id` / CLI UA） |
| `gateway/fingerprint.py` | — | 每账号独立持久化的硬件画像 |

### 新增内容

- **头族**：在原有四头之外补齐 `X-Domain` / `X-Product` / `X-User-Id` / `X-Request-ID` / `Origin` / `Referer`。UA 形态对齐官方桌面端 `WorkBuddy/<ide> WorkBuddy/<ide> CLI/<cli>`，ide 版本取该账号画像里的 `ideVersion`。
- **请求体**：补 `conversationId`（官方桌面端真实对话固有字段）。缺失时按 `machineId + 会话内容`稳定派生，保证同账号同会话稳定、跨账号不同。
- **`X-Request-ID`**：官方形态为 `sessionId + 毫秒时间戳尾段`。

### 三条设计红线

1. **画像按账号隔离**：绝不所有账号共用一套 machineId / osVersion / UA——多账号同一设备指纹正是上游并案标记的典型特征（`fingerprint.py::_allocate_profile` 本就为这件事存在，这里只消费它，不重新造）。
2. **可一键回退**：`AB_DESKTOP_FIDELITY=0` 时整套降级为原来的四头形态，上游改判据时不必改代码即可回滚。
3. **不伪造身份**：`X-User-Id` / `machineId` 等全部取自该账号自己的真实画像，不借用别人的，也不编造不存在的设备。

---

## 三、网关一致性自检第三项必错

### 问题表现

自检结果固定长这样：

```
✓ 网关健康检查 /health — HTTP 200 · 39 个模型 · 密钥校验已开启
✓ 模型清单 /v1/models — HTTP 200 · 39 个模型
✗ 密钥配置自洽性 — name 'request' is not defined
```

### 根因

`web_proxy.py` 的 `/api/gateway-selftest` 函数签名只有 `user` 参数，但第三项（密钥配置自洽性）里把 `request` 传给 `_gw_headers(request)` 去透传登录态 Cookie——`request` 在作用域里不存在，必抛 `NameError`，被外层 except 捕获后渲染成一行红字。前两步不依赖 `request`，所以一直显示绿。

### 修复

补上 `request: Request` 参数。FastAPI 按类型注解注入，与同文件其它代理端点（如 `account_pool_get_official`）写法一致。

---

## 四、账号管理页搜索框三处调整

### 4.1 昼夜不适配

- **问题**：`.wb-acc-search-box` 的底色是硬编码 `#ffffff`，深色主题下就是一坨白补丁贴在暗色页面上。
- **修复**：改用官方同款 CSS 变量 `var(--card, #ffffff)`，并补 `.dark` 兜底（`var(--popover, #1f2430)`）。`html.dark` 下官方会把 `--card` 翻成暗色，不写死颜色才能跟着主题走。

### 4.2 PC 端未对齐页面右端

- **问题**：`.wb-acc-search-wrap` 的 `margin-left` 写死 `8px`，搜索框只是「紧跟档位组右侧」。而档位容器本身是 `w-fit`（不占满整行），所以搜索框停在页面中部。
- **修复**：`margin-left: auto`。档位容器已改为横向 flex + `space-between`，auto 才会把搜索框真正推到行的最右端。移动端媒体查询里仍显式覆盖回 `margin-left: 0`（窄屏换行场景不受影响）。

### 4.3 误出现在积分统计页

- **问题**：注入函数只靠「页面上有 `article`」判定，而积分统计页 `/credit-stats`、词元统计页 `/token-stats` 同样渲染 article 卡片，于是搜索框也插了上去。用户明确「只需要在账号统计页面出现即可」。
- **修复**：新增 `wbIsAccountsPage()` 守卫，判据用 hash + path 双条件（与 `wbResolveView` 的路由规则一致）：
  - `hash === "#/accounts"` → 账号管理（明确后缀路由）
  - `path === "/"` 且无 hash → 官方根页，即账号管理的背后页面
  - 其余一律不注入。
- **附带修复残留**：新增 `wbRemoveAccountSearch()`。SPA 切页不重建 document，只靠「不注入」会让上次插的节点一直留着。该函数做三件收尾：撤掉节点、把被过滤隐藏的卡片全部放出来（否则过滤状态比 DOM 活得久，切回来会「少了一批账号」）、清掉档位容器的 `data-wb-search-row` 标记与内联样式（不清的话新节点不会获得横向布局，搜索框掉到下一行）。

---

## 五、账号卡片完全灰色无法操作

### 问题表现

其他设备报告：账号管理页的账号卡片完全灰色、无法操作。

### 根因（前端时序误判，非真实状态）

线上实况佐证：`refresh_state.json` 不存在，`accounts.json` 里 10 个账号 0 个带 `needs_relogin`——没有任何账号真需要重登。

真正的问题有两条，都指向同一个闭包变量 `wbPoolCache`：

1. **`wbReplaceReloginErrorBlocks()` 在缓存未就绪时也往下走**，并对每张卡执行 else 分支的 `wbApplyReloginEnabled(card)`——语义上把「没有数据」当成「一切正常」，会主动解除可能正确的灰化、抹掉 header badge 的隐藏状态。
2. **数据到达后没人重跑判定**。`run()` 里它是同步调用的，那一刻 `wbPoolCache` 可能还是上一个页面遗留的旧数据（SPA 切页不重建 JS 上下文，该变量跨页面存活）。旧缓存里某账号 `needsRelogin=true`，卡片当场被灰化；而 `injectAccountPool()` 的 fetch 回来后 `render()` 只重建控制条，不会再跑一次判定——卡片永久停在灰态。

### 修复

- `wbReplaceReloginErrorBlocks()` 开头增加守卫：缓存为空时整轮跳过。真正的首次判定移到 `render()` 里（数据到手后立刻跑），不会漏判。
- `wbCardNeedsRelogin()` 的注释与语义收紧为「确实查过、确实不需要重登」。
- `injectAccountPool()` 在没有账号卡片时（非账号管理页）顺手清掉遗留的 `wbPoolCache`，避免旧账号状态在错误的页面上参与判定。

---

## 六、升级说明

本版为常规升级，无破坏性变更、无配置迁移、无数据格式变化。账号数据与统计流水原样保留。

新增两个可用环境变量（均有默认值，不设即走默认行为）：

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `AB_DESKTOP_FIDELITY` | `1` | 置 `0` 可一键回退到原四头请求形态 |
| `AB_CD_CHANNEL` | `1800` | 渠道拦截的账号冷却时长（秒），原有变量 |

Unraid 用户直接更新镜像并重建容器即可，模板无需改动。
