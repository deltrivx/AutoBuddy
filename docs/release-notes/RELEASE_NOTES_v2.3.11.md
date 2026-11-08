# AutoBuddy v2.3.11 发布说明

> 发布日期：2026-11-08
> 对比基准：[v2.3.10](https://github.com/deltrivx/AutoBuddy/compare/v2.3.10...v2.3.11)

## 智能路由页二次反馈修复（8 项）

### 1. 「首选」标签闪烁
**根因**：`wbRenderTrace` 末尾把增量标记 `_wbRtSeen` 重置为空，导致每次 2.5s 轮询都整块重渲染账号列表、`.new`（首选）进入动画反复播放。
**修复**：仅在“当前选中轨迹 id”变化时才重渲染账号列表。

### 2. 取消内置智能体图标，统一回退 API Key 身份
`_wbRtRawCaller` 不再尝试 `caller_name` / `client_tag` 识别，直接返回 `wbRtKeyName(api_key)`；`wbRtCallerIcon` 恒返回 `generic-app.svg`，不再按 openclaw / claude / deepseek 等渲染内置图标。左侧节点显示的即为各 API Key 的真实可读名。

### 3. 同 API 多模型合并为一个焦点框
右侧焦点框原本按 `(caller, model)` 并列多个框。现改为按 API Key 聚合，同一 API 多个模型合并为一框，形如 `DeltrivX: hy4 + hy3`（按出现顺序去重）。

### 4. 统计文案移动端/PC 对齐
窄屏 `.rt-stats` 由居中改为左对齐（`justify-content:flex-start`），与 PC 端及页面其他板块统一；「Agent 收到的错误」缩为「Agent 错误」以适配窄屏。

### 5. 移除路由组 / 路由规则 / 账号打码 / 命中规则
用户确认这些增强功能维护成本大于收益，且取消后不再出现“国内账号却命中国际版账号分流规则”的路由失效显示（根因是旧 `auto_generate` 生成的 `client_tag` 匹配坏规则，现连同 UI 一并移除）。智能路由退回纯粹“账号池自动故障转移 + 多账号/多 KEY 间路由”，顶部「重新生成默认编排」仅刷新账号池与路由策略。
> 说明：此后请求详情里的路由原因不再显示“命中规则：XXX”，统一为“智能路由（默认全局池）”。

### 6. “实时 · 网关正在做的决定” → “实时路由”并左对齐
顶部标题精简为“实时路由”，移动端 `.rt-top` 由居中改为左对齐，与页面其他板块样式统一。

### 7. 模型窗口大小显示真实值
请求详情的上下文窗口原本硬编码“未知”，改为读取后端 `context_window` 字段（有则显示真实 token 数，无则回退“未知”）。

### 8. 多账号/多KEY 板块按钮不再铺满整行
窄屏下 `.am-tools .am-reset` 不再 `width:100%`，分组头与「路由」按钮改为左对齐、按钮自然宽度，消除文案旁大片留白。

## 升级指引

常规 `docker pull ghcr.io/deltrivx/autobuddy:latest` 后按模板重建即可；本版仅前端（web_proxy，18090）与路由逻辑变更，数据目录无结构变化。
