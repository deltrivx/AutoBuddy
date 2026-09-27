# AutoBuddy v0.9.16

> 修复侧边栏图标丢失回归、积分统计项补标题图标、Token 字段改为词元。

## 1. 修复：侧边栏图标丢失（v0.9.15 引入的回归）

### 现象

v0.9.15 把「Token 统计 → 词元统计」「设置 → 系统设置」改成功后，
**这两项左侧的图标不见了**。

### 根因

`renameNavLabels()` 在 `run()` 里排在 `initCollapse()` **之前**：

```js
function run() {
  sanitizeSidebarBrand();
  renameNavLabels();      // ← 此时官方导航项还没有 span.wb-nav-label
  ...
  initCollapse();         // ← span.wb-nav-label 是这里才包上的
}
```

而改名时写的是：

```js
var node = label || a;    // label 不存在 → node 就是 <a> 本身
node.textContent = next;  // ← 清空 <a> 的所有子节点，svg 图标一起没了
```

`setAttribute` 的 `data-wb-label` 确实改对了，但我验证时只检查了
`dataLabel` 字段（显示"词元统计"），**没有检查图标是否还在** ——
所以自动化测试通过，实际图标已丢。这是「验证指标选错」导致的漏检。

### 修复

改为**逐个文本节点**改名，svg 等元素子节点原样保留：

```js
Array.prototype.slice.call(a.childNodes).forEach(function(node) {
  if (node.nodeType !== Node.TEXT_NODE) return;   // 只动文本节点
  ...
  node.textContent = raw.replace(txt, next);
});
```

已被 `initCollapse()` 包成 `span` 的情况（后续轮次 / MutationObserver 重跑）
也一并处理。

## 2. 每日任务页积分统计四项补「标题图标」

官方积分统计页每个数据项的标签实测结构：

```
16px lucide 图标 + 8px gap + 13px / 500 文字
```

每日任务页此前**只有文字，没有图标**。现按官方结构补齐：

| 数据项 | 图标 | 形态 |
| :--- | :--- | :--- |
| 今日积分 | sparkles | 星芒 |
| 签到积分 | calendar-check | 签到日历 |
| 任务积分 | list-checks | 任务清单 |
| 总积分 | trending-up | 趋势上升 |

同时把标签容器改为与官方一致的 `flex` + `items-center` + `gap: 8px`。

## 3. Token 相关字段改为「词元」

页面内以下字段统一改为「词元」：

```
Token 与调用趋势  ->  词元与调用趋势
Token 活动        ->  词元活动
Token 统计        ->  词元统计
Token 用量        ->  词元用量
总 Token          ->  总词元
```

⚠️ **只列完整短语做替换，不做裸 `Token` 全局替换** ——
JS 里 `/api/token-stats` 这类路由与标识符含 Token，全局替换会直接把接口打挂。

## 验证

```
四模块 AST                    ✅ 通过
注入 JS node --check          ✅ 通过
13 项单元测试                 ✅ 全绿
版本一致性自检                ✅ 通过
部署后实测                    见下（含图标在位检查）
```

## 升级说明

重建容器即可，`/data` 数据不受影响。

- 无新增或删除环境变量；
- 无数据库结构变更；
- 本次为纯前端展示层调整，无后端逻辑变更。
