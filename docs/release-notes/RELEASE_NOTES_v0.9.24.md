# AutoBuddy v0.9.24

> 额度耗尽的账号由巡检自动停用，不再只提醒用户手动删除。

## 问题

额度用尽的账号**一直留在轮询里持续失败**，系统本该自己把它停掉，
却只能反复提醒用户去手动删除。

## 根因：额度耗尽根本不在自动禁用的触发条件里

账号级自动禁用只认两类：

```python
if verdict not in ("auth_failed", "restricted"):
    continue
```

而额度耗尽的上游响应是：

```
HTTP 402  {"msg":"Insufficient Balance"}              code=ACCOUNT_QUOTA
HTTP 429  {"data":{"code":14018,"msg":"Credits exhausted..."}}
```

**两条都掉进 `transient`**：

| 响应 | 归类路径 | 结果 |
| :--- | :--- | :--- |
| HTTP 402 | 不在 `UNAVAILABLE_STATUS{400,401,403,404,422,500,502,503,504}`，也不在 `TRANSIENT_STATUS{408,425,429}` → 走兜底 | `transient` |
| HTTP 429 | 明确属于 `TRANSIENT_STATUS` | `transient` |

而 `transient` 的语义就是「临时状态，**不参与自动禁用**」——
它本是为「网络抖动、上游 5xx」设计的一次性状态。

**额度耗尽是持续状态**（不充值 / 不重置不会自己好），
把它当一次性抖动，是这次故障的根源。

## 修复

新增 `quota_exhausted` 这一 verdict，并纳入自动禁用：

```python
if verdict not in ("auth_failed", "restricted", "quota_exhausted"):
    continue
```

判定覆盖三条路，任一命中即可：

**1. 语义表** —— `14018 → quota_exhausted`

实测证据（2026-09-28 Mac dsh 会话日志）：

```
429 {"data":{"code":14018,"msg":"Credits exhausted. Please visit
     the link below to purchase add-on packs..."}}
```

**2. HTTP 402** —— 按状态码判定

402 Payment Required 语义唯一，不需要依赖错误码。

**3. 文字兜底** —— `QUOTA_EXHAUSTED_HINTS`

用于响应体取不到错误码的情形，覆盖
`insufficient balance` / `credits exhausted` / `额度不足` 等。

## ⚠️ 一处刻意的取舍

**没有**把 `10001` 写进语义表。

同一个码在签到接口上是「今天已签到」
（`workbuddy2api-panel` 的 `alreadyCheckinMarkers` 实测），
含义冲突 —— 写成额度耗尽会把正常的重复签到误判成没钱。

因此 402 的额度耗尽改由**状态码**判定，判据更可靠、不会误伤。

## 界面文案

额度耗尽与「账号被拦截」区分开 —— 后者只能等，前者可以充值：

| 字段 | 文案 |
| :--- | :--- |
| label | `额度已用尽` |
| action | `充值或等待额度重置` |
| userMessage | `额度已用尽，已自动停用` |

## 自愈

无需额外改动：自动启用只放开 `verdict == "available"` 的账号，
额度耗尽不等于 available，不会被误放回；
充值 / 重置后探测恢复正常，巡检会自己把它放回来。

## 测试

`_test_model_health.py` 新增 7 项（231 → 238）：

```
[ok] 14018（Credits exhausted）归类为额度用尽
[ok] HTTP 402 归类为额度用尽（不依赖错误码，避免 10001 语义冲突）
[ok] 无错误码但文案命中，同样归类为额度用尽
[ok] 额度用尽**不是** transient（transient 不参与自动禁用）
[ok] 额度用尽的账号被自动停用
[ok] 额度用尽写入账号策略且来源为 auto
[ok] 同轮正常账号不被误停
```

```
15 项单测                      ✅ 全绿
五模块 AST                     ✅ 通过
```

## 升级说明

重建容器即可，`/data` 数据不受影响。

- 无新增或删除环境变量；
- 无数据库结构变更；
- 行为变化：巡检发现账号额度耗尽时，会**自动将其停用**（来源 `auto`），
  而不是继续让它参与轮询。充值后巡检自动放回。

## 相关：v0.9.23 已修的两项

- 新加的账号不被调用（能力矩阵收窄排除未知账号）
- 巡检探测账号池外的账号（现与日常调用同源）
