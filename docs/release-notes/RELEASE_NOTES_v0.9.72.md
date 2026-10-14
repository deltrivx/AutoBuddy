> 阶段 4：助手查询能力全覆盖 —— 只读工具从 6 个扩到 15 个，覆盖 AutoBuddy 五大功能域。

## 背景

用户确认按三阶段路线推进（阶段 4 查询全覆盖 → 阶段 5 写操作确认 → 阶段 6 主动智能），
要求「逐步执行，直到全部成功」。

## 实施前的能力面盘点

助手原有 6 个工具，而 AutoBuddy 有约 60 个端点、五大功能域。逐域核对后确认
缺口，并**先探测端点再写工具**（避免凭猜调用不存在的路径）。

> 探测中发现 `/api/gateway-selftest` 返回的是 **HTML**（前端路由而非 API），
> 因此未纳入工具集 —— 这正是先探测的价值。

## 新增 9 个只读工具

| 工具 | 覆盖能力域 | 数据源 |
|---|---|---|
| `get_account_pool` | 账号池 | 选号模式、启用/停用数、可用数、最近选号来源 |
| `get_selection_stats` | 账号池 | 各账号承接次数与占比、当前在飞请求 |
| `get_disabled_models` | 模型 | 按账号列出被禁用的模型 |
| `get_health_check` | 模型 | 巡检开关、间隔、覆盖账号数 |
| `get_account_credentials` | 账号池 | 凭据有效期、7 天内到期预警 |
| `get_task_ledger` | 每日任务 | 各成长任务累计次数与成功率 |
| `get_credit_breakdown` | 积分 | 今日签到/任务拆分、近 7 天按天趋势 |
| `get_api_keys` | 网关密钥 | Key 数量、启停、调用次数 |
| `get_notify_status` | 通知 | 渠道就绪状态、消息场景 |

## 过程中修掉的一个口径 bug

`get_disabled_models` 首版读 `/api/account-models` 的 `accounts` 字段，实测发现：

- `accounts` 是 **dict**（键为账号 ID），不是 list → 遍历取不到
- 其中的 `models` 是**可用全集**（38 个 = catalog 全集），差集恒为 0
- 于是工具输出「当前没有任何账号×模型被禁用」，而同一次响应的
  `disabledTotal` 却是 **354** —— 口径完全对不上

**修正**：改读权威来源 `/api/account-pool` 的 `modelPolicy`
（`model_policy.as_model_lists` 的投影：账号 → **被禁用**的模型名列表）。
修正后实测 `23 个账号、共 354 个组合`，与 `account-models.disabledTotal`
**完全一致**。

## 验证

- 9 个新工具逐个对**线上真实端点**取数，全部正常（9/9）
- 口径核对：禁用总数 354 = 354 ✅
- 全部只读：无 `wb-daily/run`、`checkin/all`、`abort` 等写端点

## 测试

`_test_assistant.py` 更新并新增（143 → **154**）：

- 工具数契约 6 → 15
- 阶段 4 九个工具逐一断言已注册
- 阶段 4 工具**全部只读**（无写端点）
- 禁用明细必须读 `modelPolicy`（防回退到可用全集）

全量 **40 个测试文件全绿**；Python 语法通过。

## 升级说明

重建容器即可。`/data` 数据不受影响；助手配置（provider/模型）沿用既有设置。
