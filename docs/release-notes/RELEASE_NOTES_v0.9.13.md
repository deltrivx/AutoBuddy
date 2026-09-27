# AutoBuddy v0.9.13

> 每日任务页控件（按钮 / 输入框 / 下拉 / 表格）统一到官方规格。

## 背景

v0.9.12 已对齐容器宽度与标题。本版继续**细化到页面内每个控件**，
与官方页面（账号管理 / 积分统计 / Token 统计）的实测值逐项对齐。

## 对齐明细

| 控件 | 官方实测 | 每日任务页（改前） | 改后 |
| :--- | :--- | :--- | :--- |
| 主按钮 | 14px / 500 / `radius:10px` / 高 32px / 边框 1px | **11px / 400 / `radius:999px`（胶囊） / 高 25px** | 14px / 500 / 10px / 32px |
| 次按钮（刷新等） | 12px / 500 / 高 28px | 与主按钮未分档 | 12px / 500 / 28px |
| 输入框 / 下拉 | 12px / `radius:10px` / 高 32px | 12px / **`radius:6px`** / 高 30px | 12px / 10px / 32px |
| 表格 | 12px，`th` 弱化色、`padding:6px 10px` | 12px，`padding:3px 6px`（偏密） | 12px / `6px 10px` |

实测数据来源：浏览器读取官方页面 computed style —— 拿精确值，
而非目测比对。

## 实现说明（重要）

`.wb-api-btn` 是**跨页面共用**的类 —— 设置页、账号卡片等 **36 处**都在使用。
如果直接改它的全局定义，会波及其它所有页面的既有样式。

因此本次改动全部用 `.wb-daily-wrap` 作用域限定：

```css
.wb-daily-wrap .wb-api-btn { ... }
.wb-daily-wrap .wb-api-row .wb-api-btn { ... }
.wb-daily-wrap input[type="number"],
.wb-daily-wrap select { ... }
.wb-daily-wrap table, .wb-daily-wrap th, .wb-daily-wrap td { ... }
```

**只影响每日任务页**，其它页面不受影响。

## 验证

```
四模块 AST                    ✅ 通过
注入 JS node --check          ✅ 通过
13 项单元测试                 ✅ 全绿
版本一致性自检                ✅ 通过
```

## 升级说明

重建容器即可，`/data` 数据不受影响。

- 无新增或删除环境变量；
- 无数据库结构变更；
- 本次为纯前端展示层调整，无后端逻辑变更。
