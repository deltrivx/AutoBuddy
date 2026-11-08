# AutoBuddy v2.3.6 发布说明

> 对比基准：[v2.3.5](https://github.com/deltrivx/AutoBuddy/compare/v2.3.5...v2.3.6)

## 修复

- **路由页来源区「按 API 分类」从未真正渲染（v2.3.5 功能失效根因）**：`wbRtKeyName` 原先定义在 `wbLoadRoutingData` 函数内部，却被平级函数 `wbRenderTrace` 调用，跨函数作用域不可见，渲染来源节点时抛 `ReferenceError`，导致 `srcs.innerHTML` 赋值失败（保持空）、而前面的 `classList.toggle("many")` 已执行——表现为「`.many` 类在但内容空」。现把 `wbRtKeyName` 提升为全局函数，来源区正确显示 Default / iOS-App / Web-Client 等多个 API 名字节点。
- **移动端账号行 API 标签与状态文字重叠**：账号行原用 `grid` 只定义 3 列却生成 5 个子节点，窄屏下 API 标签（`rt-api-tag`）与状态文字（`首选/候选/应答`）挤在同一格内重叠。现改用 `flex` 布局 + `.rt-acct-main` 包裹「名字+标签+模型」，状态独立成行并 `flex-wrap` 自动换行，移动端不再重叠。

## 验证

通过 CDP 浏览器 + gemini-3-flash 多模态视觉核验：
- 来源区正常显示 Default / iOS-App / Web-Client 多个 API 名节点
- 账号行 API 标签与状态文字不再重叠
- 深色主题统一、无半深半浅突兀区块
- 移动端（390px）无横向溢出、无重叠混乱
- 历史列表 tabs（全部/按模型/按会话/按 API）清晰可见
