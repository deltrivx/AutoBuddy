> 修复账号卡片点「启用」时按钮跑到卡片底部（flex 折行点随按钮文案长度漂移），并补齐注入按钮 innerHTML 路径缺失的 `type="button"`（移动端整页刷新）。

## 一、更新摘要

本版（v1.0.17）处理 2026-10-20 工单第 2 件「账号卡片启用时按钮跑到卡片底部、移动端页面刷新」。这是同一个症状在 v1.0.5 之后第二次报，本版给出根因并根治，同时补掉 v1.0.5 测试覆盖留下的盲区：

1. **按钮不再跑位**：账号池控制条的 flex 折行点不再随启用/停用状态漂移。
2. **模型区「全部恢复」按钮不再被挤下去**：与账号池控制条同一个病因，一并修。
3. **移动端不再整页刷新**：9 处 innerHTML 注入的按钮补上 `type="button"`。
4. **16 项新契约断言**：把上面三条焊死在测试里，改坏即红。

## 二、根因

### 1. 按钮跑到卡片底部

账号池控制条 `gateway/web_proxy.py` 的 `.wb-pool-bar` 是 `flex-wrap: wrap`，子元素为：

```
[启用/停用按钮] [设为首选] [检测账号] [已调用 N 次] (可选)[最近调用]
```

启用/停用按钮有三种文案，**长度各不相同**：

| 状态 | 文案 | 字数 |
| :--- | :--- | :--- |
| 不在白名单 | 不在账号池 · 点击启用 | 9 |
| 账号级停用 | 已停用 · 点击启用 | 7 |
| 正常参与 | 参与调用 · 点击停用 | 8 |

点「启用」时 `wbReplacePoolBar()` 会**就地重建**这根条（v1.0.5 引入的正确行为）。主按钮一换文案，宽度随之改变，**flex 折行点跟着漂移**，后面的按钮与计数就被甩到下一行 —— 用户看到的就是「按钮跑到卡片底部」。

移动端（390px 视口、约 310px 内容宽）上 4~5 个元素本来就要折行，这个效应最明显；PC 上内容宽约 334px，多数账号刚好一行放得下，所以「时有时无」。

**为什么 v1.0.5 没修住**：v1.0.5 修的是「重建的 bar 一律 `appendChild` 到 section 末尾，与官方 React 异步内容竞争位置」—— 那一条确实修好了（`wbReplacePoolBar` 现在是 `insertBefore + remove` 原位替换，27 张卡实测 0 处错位）。但**折行点漂移**是另一条独立病因，v1.0.5 的三处修复都没有覆盖到。

### 2. 移动端整页刷新

v1.0.5 给 `document.createElement("button")` 路径的 14 处按钮统一补了 `.type = "button"`，并写了反向断言。但仓里还有一批按钮是 **innerHTML 字符串**注入的，那条路径完全没有覆盖：

| 位置 | 按钮 |
| :--- | :--- |
| 登录弹窗 | 登 录 |
| 每日任务面板 | 中止 / 保存配置 / 立即执行 / 全部执行任务 / 全部立即签到 / 刷新（任务）/ 刷新（执行） |

这些按钮落在官方 `<form>` 内时默认 `type=submit`，点一下即触发表单提交整页刷新。移动端窄屏下按钮更容易被官方 form 包裹，命中率比 PC 高。

## 三、改动清单

只改 `gateway/web_proxy.py`（CSS）与 `_test_v105_ui_fix.py`（断言），**未改任何 DOM 结构、未改任何后端逻辑、未碰账号数据**。

| 选择器 / 位置 | 改动 | 作用 |
| :--- | :--- | :--- |
| `.wb-pool-btn` | `+ flex: 0 0 auto; + white-space: nowrap` | 按钮不压缩变形、不自行断行 |
| `.wb-pool-bar > .wb-pool-btn:first-child` | `+ min-width: 118px; + text-align: center` | 启用/停用三种状态宽度取齐，折行点不跳 |
| `@media (max-width: 640px)` 内同名规则 | `min-width: 104px`；`.wb-pool-bar { gap: 6px }`；`.wb-pool-btn { padding: 1px 8px }` | 窄屏按钮组尽量留同一行 |
| `.wb-pool-last` | `+ margin-left: auto; + flex: 0 0 auto; + white-space: nowrap` | 「最近调用」靠最右 |
| `.wb-pool-count` | `+ flex: 0 0 auto; + white-space: nowrap` | 「已调用 N 次」锁定不压缩 |
| `.wb-am-restore` | `+ flex: 0 0 auto; + white-space: nowrap` | 模型区「全部恢复」不被徽标挤下去 |
| 9 处 innerHTML `<button>` | `+ type="button"` | 防 form 内默认 submit 整页刷新 |

## 四、实测

- **PC（1440 视口）**：27 张账号卡全量 DOM 审计，`wb-pool-bar` 与 `wb-am-box` 相对位置 0 处错位；「不在账号池 · 点击启用 → 参与调用 · 点击停用」与「全部恢复」两条路径用 fetch 拦截做了真实点击（后端未收到任何写请求，账号数据零改动），重绘后卡片子节点顺序与点击前完全一致，按钮数量不变。
- **移动端**：按钮 `type="button"` 覆盖经静态断言 100% 验证（innerHTML 路径 + `createElement` 路径双查）；窄屏 CSS 规则经断言锁定。
- **全量回归**：58 个测试文件全部通过；`_test_v105_ui_fix.py` 由 19 项增至 35 项。

## 五、配置与兼容性

- **零配置升级**：无新增配置项，无破坏性变更，升级容器即生效。
- **未触碰**：账号池数据、模型禁用策略、风控标记、通知配置均未改动。

## 六、升级方式

```bash
docker pull ghcr.io/deltrivx/autobuddy:v1.0.17
# Unraid：php /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/rebuild_container AutoBuddy
curl -s http://127.0.0.1:18091/health   # version 应为 1.0.17
```
