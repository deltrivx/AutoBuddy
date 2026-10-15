> 一次修掉设置页三个 UI 缺陷：**「智能助手」区块完全不显示**、**模块顺序偶发随机**、**侧边栏切换闪屏**。

## 一、设置页「智能助手」配置不见（最严重）

### 症状

设置页只有 6 个区块：外观 / 账号资料 / 通知 / 模型巡检 / API 接入 / 关于。
**唯独少了「智能助手」**，刷新、清缓存、重启容器都不出现，且控制台**没有任何报错**。

### 根因：两处 `var` 作用域错误，异常被静默吞掉

`injectAssistant()` 的调用链末端是 `.catch(function () {})` —— 一个空的 catch。
渲染函数一旦抛异常，用户侧看不到任何痕迹。

**第一处：`renderFields()` 在 `state` / `draft` 初始化之前被调用**

```js
var fieldsBox = wbEl("div", null);
card.appendChild(fieldsBox);
renderFields(fieldsBox, cur);      // ← 在这里调用

var state = { ... };               // ← 还没赋值
var draft = {};                    // ← 还没赋值

function renderFields(box, prov) {
  var slot = provs[prov.key] || {};
  var d = draft[prov.key] || {};   // ← draft 是 undefined → 抛错
```

`var` 会提升声明，但**不会提升赋值**。调用 `renderFields()` 时 `draft` 仍是
`undefined`，函数体第一行就炸：

```
TypeError: Cannot read properties of undefined (reading 'gateway')
```

> **为什么这个错误信息极具误导性**：`prov.key` 恰好等于字符串 `'gateway'`
> （网关 provider 的 key），于是报错读起来像「谁去读了 `data.gateway`」。
> 实际它与 `/api/api-keys`、`/api/assistant` 的 `gateway` 字段毫无关系，
> 只是数组下标。排查时反复在这个方向上空转。

**第二处：按钮变量声明在 `renderFields` 内部，却在外部使用**

```js
function renderFields(box, prov) {
  ...
  var actBox = wbEl("div", null);   // ← var 只提升到 renderFields
}                                   // ← 出了函数就没了

actBox.appendChild(testBtn);        // ← ReferenceError
```

这是 v0.9.84 既有的缺陷，一直被第一处掩盖（第一处先炸，根本走不到这里）。

### 修法

- `actRow` / `actMain` / `actBox` 提到外层作用域；
- `renderFields(fieldsBox, cur)` 移到 `state` / `draft` 初始化**之后**；
- 两处都补注释，写明「为什么不能提前 / 不能放里面」，防止再被改回去。

### 定位过程（本次换成实证，不再读栈猜）

上一轮排查失败在「读 V8 栈行号 → 推行号」：V8 报的是**函数入口行**不是抛错行，
且页面行号与源码行号有偏移，反复推出错误结论。本次改为：

1. 把 `injectAssistant` 的空 `catch` 改成 `console.error(e.stack)`，让错误自己说话；
2. 拿 CDP 连真实 Chromium 刷新页面，读回完整 4 帧调用栈；
3. 用服务端实际输出的 HTML 做行号校准（源码行 = 文档行 + 偏移），逐帧映射。

---

## 二、设置页模块顺序偶发随机

### 症状

「API 接入」和「模型巡检」两个区块的位置会互换，有时要刷新几次才恢复正常。

### 根因

六个区块都是「fetch 一回来就 `appendChild`」：

```
injectApiAccess()      fetch("/api/api-keys")      → 回来就 append
injectModelHealth()    fetch("/api/model-health")  → 回来就 append
```

谁的网络先返回，谁就排在前面。`run()` 里的调用顺序保证不了最终 DOM 顺序。

### 修法

新增 `wbSortSettingsSections()`，每次挂载后按固定表重排：

```
外观 → API 接入 → 模型巡检 → 智能助手 → 通知 → 账号资料 → 关于
```

结果与网络时序完全无关。

**一个坑**：宿主容器不能拿排序表的第一个 id 反推 —— 官方把「外观」单独包在
`DIV.px-1` 里，拿它反推会得到只有 1 个子节点的错误容器，排序被静默跳过
（第一版就是这么写的，实测无效）。改为选「装着最多注入区块」的容器。

**另一个坑**：重排必须做「已有序就一个节点都不动」的前置判断，否则
`appendChild` 会产生新的 DOM 变更 → 被 observer 接住 → 再触发 `run()` → 死循环。

---

## 三、侧边栏切换闪屏 / 卡顿

### 根因：自激反馈

CDP 实测一次「设置 → 账号管理」切换：

| 指标 | 实测值 |
| :--- | :--- |
| mutation 批次 | 31 批 |
| mutation 条数 | 425 条 |
| 由此跑出的 `run()` | **31 轮** |
| long task 占用 | 1030ms |
| 最长帧间隔 | 2077ms |

链路：

```
React 分批重渲染 → 拔掉我们注入的节点（wb-pool-bar / wb-am-box）
  → observer 唤醒 run() → 我们重新注入 → appendChild 又触发 observer
  → 再 run() ……
```

官方 React 的分批渲染只贡献了**第一次**，后面 30 轮全是我们自己打自己。
侧边栏 DOM 本身 87 帧逐帧比对 0 跳变 —— 用户看到的「闪」其实是主线程卡死造成的掉帧。

### 修法（三层）

1. **去抖窗口** 1 帧（~16ms）→ 120ms，把同一轮渲染的多个批次合并；
2. **observer 只订阅 `childList`**，不再订阅 `attributes` —— 官方高频刷的
   `class` / `aria-*` / `disabled` 变化（占 425 条里的绝大多数）不再唤醒 `run()`；
3. **路由静默期（2.6s）+ 不对称过滤**：
   - 我们自己 `append` 的节点 → 不唤醒（这是我们刚注入的）
   - 官方拔掉我们的节点 → **仍然唤醒**（否则注入会静默丢失）
   - 只删不增的 record 一律唤醒，保守但不会丢功能

### 效果

| 指标 | 修复前 | 修复后 |
| :--- | :--- | :--- |
| 一次切换跑出 `run()` | 31 轮 | 1~2 轮 |
| p95 帧间隔 | 224ms | 92ms |

A/B 对照实验（同一页面、同一切换动作，各跑一遍）：

| 配置 | long task | p95 帧间隔 |
| :--- | :--- | :--- |
| 本版修复 | 1215ms | 224ms |
| 完全禁用我们的 observer | 1064ms | 92ms |
| 完全禁用我们所有 JS | 1138ms | 74ms |

剩余卡顿大头在官方 React 框架（占 CPU 78%），不在本次改动范围内。

---

## 四、验证

### 单元测试

47 个测试文件全部通过，0 失败（含 `_test_webui_panels.py` 62 项、
`_test_assistant.py` 161 项、`_test_version_sync.py` 13 项）。

### 真机验证（CDP 连真实 Chromium，非服务端自检）

连续 4 轮「刷新设置页 → 切到账号管理 → 切回设置页」，每轮检查：

| 检查项 | 结果 |
| :--- | :--- |
| 智能助手区块存在 | ✅ 4/4 轮 |
| 助手字段渲染完整 | ✅ 8 个字段（4/4 轮） |
| 模块顺序固定 | ✅ 4/4 轮，与 `WB_SETTINGS_ORDER` 完全一致 |
| 切回后顺序仍正确 | ✅ 4/4 轮 |
| 账号池控制条注入 | ✅ 22 个（4/4 轮） |
| 已用模型区注入 | ✅ 22 个（4/4 轮） |
| 侧边栏导航标签 | ✅ 5 个（4/4 轮） |
| 控制台报错 | ✅ 0 条 |

---

## 升级说明

```bash
docker compose pull
docker compose up -d
```

本次无新增环境变量、无配置变更、无数据库迁移。
