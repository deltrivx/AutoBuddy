账号页搜索框重构为独立过滤行（官方 DOM 零改写），移动端吸顶 + 防 iOS 聚焦缩放。

## 变更

### 账号页搜索框锚点重构：不再寄生官方档位容器

旧实现（`gateway/web_proxy.py` 的 `wbInjectAccountSearch()`）把搜索框插进
官方「档位容器」内部，靠**内联改写官方节点的布局**（flex-col → flex-row +
justify-between）实现同行右端对齐，因此需要一整套 `wbSearchRow` dataset 标记
+ 撤销时清理官方节点内联样式的机器 —— 官方一改版最先坏（历史上已为它修过
一次「搜索框掉行」）。

现改为在**页头行（`div.flex.items-start.justify-between`）之后插入独立过滤行
`wb-account-search-row`**（`closest()` 只读祖先定位，兜底档位容器父级）：

| 端 | 位置 |
| :--- | :--- |
| PC（≥640px） | 档位行正下方独立行、搜索框右对齐 —— 视觉位置与旧版几乎一致 |
| 移动端（<640px） | 全宽过滤行 + `position: sticky` 吸顶，长账号列表滚到中段也能直接改过滤词 |

官方 DOM 零改写、零残留：撤销时只删自己的两个节点，`wbSearchRow` 内联改写
与清理逻辑整体移除。

## 修复

- **移动端 iOS 聚焦自动放大**：输入框字号 12px < 16px，iOS Safari 聚焦时必
  自动 zoom 整页且不回弹。移动端输入框字号提到 **16px**、框高提到 **40px**
  （触控目标），`type="search"` 换上移动端原生清除按钮；吸顶行补实体背景
  （跟随 `--background`，深色主题兜底），避免滚动时卡片从框底透出。
- PC 端视觉尺寸契约保持不变（36px 行高 / 28px 框高 / 10px 圆角 / 12px 字号 /
  右端对齐），昼夜主题适配沿用 `--card` / `--popover` 变量。

## 测试

- `tests/legacy/test_v1010_features.py` 需求 1 契约同步到 v1.0.35：锁定
  「独立过滤行 + afterend 注入 + 官方节点零改写（`wbSearchRow` 不得回归）」、
  移动端吸顶 / 16px / 40px / type=search 五条新契约，PC 尺寸四条原样保留。
- 全量 `tests/`、`tests/legacy/` 通过。
