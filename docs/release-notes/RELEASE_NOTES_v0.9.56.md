> 通知管理前端对齐 MoviePilot：渠道卡片网格 + 点击弹配置弹窗（每渠道独立启用 / 消息场景 / 测试 / 确认）。

## 背景

用户反馈：前端 UI 配置一直没对齐 MoviePilot。核实其源码（MoviePilot-frontend 的
`AccountSettingNotification.vue` + `NotificationChannelCard.vue` +
`NotificationChannelInfoDialog.vue`）后确认真实模型，此前"一张长表单平铺所有字段"不是 MP 的做法。

## MoviePilot 的真实 UI 模型（对齐项）

| MP 组件 | 对齐后 |
|---|---|
| 渠道以**卡片网格**呈现（VBadge 启用绿点 + 类型名 + 点击弹窗） | ✅ 卡片网格：启用徽标（绿点）+ 就绪状态 + 场景摘要，点击弹配置 |
| `NotificationChannelInfoDialog` 配置弹窗（启用开关 / 消息场景 / 渠道字段 / 底部确认） | ✅ 弹窗：独立启用开关 / 消息场景标签多选 / 注册表驱动字段 / 测试 + 确认 |
| `NotificationConf.enabled` 每渠道独立启停 | ✅ `_enabled`（后端已支持，send 跳过停用渠道） |
| `NotificationConf.switchs` 消息场景订阅 | ✅ `_scenes`（SCENES 清单 + 渠道级订阅，空=全部） |

## 后端配套

- `public_view()` 输出每渠道 `_enabled` / `_scenes`；`SCENES` 场景清单
  （每日任务完成 / 账号余额不足 / 巡检结果 / 系统异常）；
- `update_config()` 接受 `_enabled` / `_scenes`；
- `send()` 跳过停用渠道（`send_test` 显式测试不受限）。

## 测试

- `_test_notify.py` 扩至 31 项：渠道级停用回读、场景订阅回读、
  send 跳过停用渠道、send_test 不受限、SCENES 非空。
- 全量 32 个测试文件全绿；Python 语法 + 内嵌 JS `node --check` 通过。

## 升级说明

重建容器即可。设置页 → 通知管理：渠道以卡片呈现，点击卡片弹出配置
（启用开关 / 消息场景 / 凭据 / 测试 + 确认）。
