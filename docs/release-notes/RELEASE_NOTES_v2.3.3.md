# AutoBuddy v2.3.3 发布说明

> 发布日期：2026-11-07
> 对比基线：[v2.3.2](https://github.com/deltrivx/AutoBuddy/compare/v2.3.2...v2.3.3)

## 修复

- **智能路由页 SPA 内 hash 切换无法选中**：原先进入 `#/routing` 仅在视图不存在时创建一次，之后仅切换 `display`，导致在已打开的会话里从其它页切回路由页时视图残留失效状态、选择器点不动（整页加载/新标签页正常）。改为每次进入都销毁旧视图并重新 `wbCreateRoutingView()`（走 innerHTML + 事件委托，重建无副作用），补齐会话内切换路径。
