本版补齐外部评估里剩下的两条真缺口：**单账号风控预警推送**与**打卡事件时间戳抖动**。
两条都不碰账号数据、不新增任何自动处置动作。

评估的八条建议里，另外五条经代码核对后判定为已有覆盖或风险过高，本版不做：
Cookie 生命周期（本仓走 OAuth refresh token 轮转，续约链完整）、非 root 降权（闭源引擎硬编码写
`/data/.wb-switch/`，降权即瘫）、TLS 指纹对齐（三条签到链刚出过事，换 requests 层风险高收益低）、
大 Token 硬性限流（v1.0.9 的 8000 分桶软性调度已在做，硬闸门会拒掉巨型 Prompt）、
每日冷备 cron（备份链已存在，真缺的是 rsync 漏备软链，属宿主机侧）。

---

## 新增：单账号风控预警推送

**改了什么**：`gateway/cooldown.py` 新增滑动窗口失败计数与预警回调；`gateway/main.py` 注册
`_risk_alert()` 回调；`gateway/notify.py` 登记新场景 `account_risk_alert`。

**阈值**：10 分钟窗口内失败 **3 次**（`AB_CD_RISK_WINDOW` / `AB_CD_RISK_THRESHOLD` 可覆盖），
同一账号 1 小时内不重复推送。窗口外的历史命中自动滑出，状态文件不会无限膨胀。

**推送内容**：账号昵称、窗口内失败次数、最后一次上游状态码，以及一句明确的
「已自动施加冷却，但未做任何自动停用」。

**与既有告警的分工**：`refresh_guard_alert` 管 refresh token 批量失败（上游 session 被重置），
本场景管单账号调用侧的限流/拒绝迹象，两者互不重叠。

### 红线说明

**只通知，不自动停用。**

`autoDisableAccounts` 维持关闭。本次改动没有任何写账号池的代码路径：`cooldown.py` 只读写自己的
`cooldown_state.json`，`main.py` 的回调只读账号池取昵称（60s 短缓存，只读不写回）。
账号是否停用只由用户在界面人工决定 —— 自动停用正是此前误封 16 个号的元凶。

回归测试 `_test_risk_alert.py` 里有一条专门扫描 `cooldown.py` 可执行代码的红线断言：不 import
`notify`、不 import `account_policy`、不含账号池写入调用、不含停用调用面。

---

## 变更：打卡事件时间戳抖动

**改了什么**：`gateway/vendor/workbuddy_daily.py` 新增三个常量
`EVENT_DRIFT_MIN_MS=80` / `EVENT_DRIFT_MAX_MS=400` / `REPORT_DELAY_MAX_MS=300`。

**之前**：一次批量上报的所有事件都取 `int(time.time() * 1000)` 现算，`reportDelay` 恒为 `0`，
`chat_request_events()` 的三个事件时间戳是固定的 `now` / `now+50` / `now+100` 等差 ——
在服务端眼里就是**同一毫秒齐落**，真人不可能这样操作。

**现在**：
- 相邻事件时间戳间隔在 80~400ms 内**随机**取，按批次序号累加，保证严格递增；
- `reportDelay` 给 `[0, 300)` 的随机整数；
- 事件**自带** `timestamp` 时尊重原值不覆盖（`chat_request_events` 已排好递增序列，
  覆盖会毁掉它），只有调用方没给时才由 `report()` 补；
- `mp_report()` 同口径兜底。

**为什么压在 1s 量级内**：签到链有超时限制，抖太大会把事件推到窗口外。v1.0.9 的 Jitter 只覆盖
重试退避与选号防抖，没覆盖事件时间戳，本版补这一层。

回归测试 `_test_event_jitter.py` 锁定四条契约：三事件时间戳严格递增、相邻间隔不固定、
单个间隔落在 [80, 400] ms 内、整批跨度 < 1s。

---

## 升级注意

- 本版**无需改动配置**，两个阈值都有默认值，想调再走环境变量。
- 预警推送依赖通知总开关已开启（`notify` 配置里的 `enabled`），否则 `send_scene` 直接返回
  「通知总开关未开启」，不影响任何主流程。
- 事件时间戳抖动是客户端侧行为，不影响签到结果与积分到账。
