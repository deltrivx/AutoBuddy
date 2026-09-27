# AutoBuddy v0.9.11

> 签到积分改从签到状态取真实奖励，积分统计栏 1:1 复刻官方结构。

## 1. 签到积分终于统计正确

### 问题

你反馈：**「今日签到肯定是有积分的，但是你都算成任务积分了」** —— 完全正确。

### 根因

积分聚合原先只从 `wb_daily_tasks` 的 `detail` 文本解析「`+N积分`」。
而 checkin 行的 detail 是这样的：

```
[23:47:14][账号5]    ✅签到: 今天已签到，请明天再来
```

**实测 18 条 checkin 记录全部不含任何数字。** 所以签到奖励从未被计入，
全部积分都被归到了「任务积分」这一类。

### 真实来源

签到奖励在每个账号的**签到状态**里：

| 字段 | 含义 |
| :--- | :--- |
| `raw.daily_credit` | 每日签到奖励额 |
| `raw.checkin_dates` | 历史签到日期列表 |

### 修复

`/api/wb-daily/credit-summary` 现在会读取**已缓存**的 `/api/checkin/status`
（60 秒 TTL，复用既有缓存，**不额外打上游**），把签到奖励并入：

- 今日签到积分（今日日期在 `checkin_dates` 中的账号相加）
- 总积分
- 按天明细中的 `checkin` 列

### 实测数据

```
7 个今日已签到账号 × daily_credit 100 = 700 分签到积分
```

（另有 4 个账号的上游不支持签到状态接口，不参与统计。）

## 2. 每日任务页「积分统计」栏 1:1 复刻官方结构

### 问题

上一版虽然改用了官方的字号（26px / 600），但**布局仍是自造的**
`flex-wrap` + 四项各自着色，所以看起来并没有对齐。

### 修正

按官方 `/credit-stats` 的**真实 DOM 结构**（浏览器实测）重写：

| 层级 | 官方实测值 |
| :--- | :--- |
| 卡片 | `rounded-2xl` + 1px 边框 + `bg-card/70` + `overflow:hidden`，`flex-col` |
| 网格 | `grid grid-cols-1 sm:grid-cols-4`，≥640px 时 `py-5`（20px 0） |
| 单项 | `flex flex-col items-center justify-center px-4 py-3/5 text-center`，非首项左边框 |
| 标签 | 13px / 500 / `leading-20px` / `muted-foreground` |
| 数值 | `mt-3`(12px) / 26px / 600 / `leading-32px` / `tracking -0.025em` / `tabular-nums` |

关键差异：**官方用的是 grid，不是 flex-wrap**。这是上一版没对上的根本原因。
同时不再给四项数值各自着色，与官方一致统一为前景色。

> 说明：样式取自浏览器实测 DOM 与 computed style，而非截图比对。

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
- 签到积分依赖 `/api/checkin/status` 的既有缓存：首次访问若尚未预热，
  签到积分会短暂显示 0，片刻（缓存建立后）自动补全。
