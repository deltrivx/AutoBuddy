# AutoBuddy v2.3.12 发布说明

> 发布日期：2026-11-08
> 对比基准：[v2.3.11](https://github.com/deltrivx/AutoBuddy/compare/v2.3.11...v2.3.12)

## 智能路由页与系统设置页跨页样式对齐

用 CDP（Chrome DevTools Protocol）驱动真实生产页面，分别读取设置页与路由页的计算样式后发现：两页虽已统一 1180px 居中容器与 28px/600 主标题，但**卡片内部 token 不一致**——设置页主卡片为 `border-radius:16px / padding:20px`，路由页注入卡片为 `border-radius:10px / padding:10-12px`，整体观感偏“紧、密”，与官方设置页的“宽松板块”对不上。

本次将路由页主卡片（`.wb-rt-card` 各子类、实时轨迹 `.rt` 舞台）统一为 `border-radius:16px / padding:20px`，页面标题 `.am-t` 由 19px/650 改为 28px/600（与设置页 `h1` 一致），内容容器 `.wb-rt-wrap` 边距对齐设置页（`36px 24px`）。移动端卡片内边距同步由 12px 提到 16px。
