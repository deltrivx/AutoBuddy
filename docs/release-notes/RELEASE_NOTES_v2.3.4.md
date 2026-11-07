## [v2.3.4] - 2026-11-08

<!-- summary: 智能路由页视觉风格完全照搬 Magpie（硬编码 rgb 设计令牌 / dashed 虚线账号卡片 / 10px 圆角 / 统计数字移至舞台右上角 / 连线连续修复），并用多模态视觉模型逐屏比对验收，视觉一致度 8/10 -->

### 变更

- **智能路由页视觉风格完全对齐 Magpie**：原先路由页沿用全局主题（oklch 现代卡片风），与 Magpie 的浅色 rgb 设计语言差异明显。现于 `#wb-routing-view` 作用域硬编码 Magpie 真实设计令牌（`--background #f4f4f6`、`--foreground #1c1c21`、`--card #fff`、`--border #e3e3e8`、`--primary #4f46e5`、`--muted #85858d`、`--green #189a58`、`--red #d64545`、`--font -apple-system…`、`color-scheme: light`），账号卡片边框由 `solid` 改为 Magpie 招牌的 `dashed` 虚线，卡片/节点圆角统一收敛到 `10px`，统计数字从页面底部移到舞台右上角（Magpie 同款位置），并对齐卡片内边距与字体族。
- **实时轨迹连线修复**：选中账号的紫色高亮连线原先用 `stroke-dasharray` 虚线被误判断开，改为实线并在底层加浅色光晕，保证任意背景下连续可见；舞台节点间距收窄、连线加粗加深。
- **移动端响应式收敛**：卡片圆角与嵌套边框收敛、标题区 padding/副标题精简，移动端无横向溢出（DOM `scrollW=winW` + 视觉确认）。
