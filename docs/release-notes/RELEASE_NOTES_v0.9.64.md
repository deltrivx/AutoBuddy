> 调度算法三项优化：配额账本时间衰减、sticky 记账+配额仲裁、解析上游 Retry-After。

## 背景（对比 Smart Load Balancer 后的定位）

用户要求「测试算法是否正常，对比 Smart Load Balancer 项目是否需要优化」。

实测数据暴露三个问题：

| 问题 | 实测数据 |
|---|---|
| `credit_picks` 纯累计、**永不衰减** | 重启才清零；相关系数在 +0.032 ~ +0.083 反复摆动 |
| sticky 占 96% 且**不记账** | auto 只服务 4% 流量；吃满粘性流量的账号比值反而偏低 → 正反馈 |
| 冷却时长靠猜，**不读 Retry-After** | 429 软冷却固定 600s 起，上游给了准确时长却不用 |

对比对象：**Azure/aoai-smart-loadbalancing**（77⭐）——其核心正是「感知 429 +
Retry-After，按上游承诺精确等待 + 立即换后端」。我们已对齐「立即换号」，
缺的是 Retry-After。

## 实施

### ① 配额账本时间衰减

`credit_picks` 从纯累计计数器改为 `{n, t}` 指数衰减账本（半衰期
`AB_CREDIT_PICK_HALFLIFE`，默认 1800s）：

- 配额只反映**近半个衰减周期**的公平性，早期抢占不再永久压制后来者；
- 重启影响显著变小（旧记忆自然淡出，无需清零）；
- 附带剪枝，账本不随账号池增删无限增长。

### ② sticky 记账 + 配额仲裁

- sticky 命中**也记入同一账本**（此前不记，导致吃到粘性流量的账号比值偏低、
  更容易被 auto 选中，形成正反馈）；
- 新增仲裁条件：绑定账号「有效已选 / 配额」劣于最优候选超过
  `AB_STICKY_QUOTA_FACTOR`（默认 1.5）倍时解绑走配额；
- 原「余额掉队」条件保留（`AB_STICKY_YIELD_RATIO`）。

### ③ 解析上游 Retry-After

新增 `cooldown.parse_retry_after()`，支持秒数与 HTTP 日期两种合法形式；
转发路径把 `res.headers` 透传给冷却账本。

与退避取**更晚**者（保守——上游说 5s 就照做很可能立刻白撞），并封顶
`SOFT_RATE_MAX`（2h）防异常值把账号关太久。

## 测试

新增 `_test_sched_decay_retryafter.py`（38 项）：

- 源码契约：衰减常量/助手/剪枝、sticky 记账与仲裁、Retry-After 解析与采纳；
- **真实模块行为**：`parse_retry_after` 秒数/日期/空值/垃圾值；
  `record_failure(retry_after=900)` → 冷却约 900s；异常大值封顶；
  不传则退回默认 600s；
- 衰减公式数值验证：1 → 0.5 → 0.25 → 0.125，时间倒流不放大。

同步更新两个既有测试锁定新契约：`_test_credit_weight_sched.py`、
`_test_sticky_yield.py`。

全量 **37 个测试文件全绿**。

## 新增可调参数

| 变量 | 默认 | 作用 |
|---|---|---|
| `AB_CREDIT_PICK_HALFLIFE` | 1800 | 配额记忆半衰期（秒） |
| `AB_STICKY_QUOTA_ARB` | 1 | sticky 配额仲裁开关 |
| `AB_STICKY_QUOTA_FACTOR` | 1.5 | 让位倍数阈值 |

## 升级说明

重建容器即可。`/data` 数据不受影响；重启后旧累计计数自然淡出，无需手工清理。
