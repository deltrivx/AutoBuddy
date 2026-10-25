并入另一设备 v1.0.16 补丁镜像的 5 项有效补丁：账号池归零告警、TTFB 埋点接线、autoEnableAccounts 白名单、refresh_guard 落盘加锁、签到推送补今日新增/消耗。

## 新增

### 账号池就绪探针（优化9）

`gateway/main.py` 的 `account_pool_ready_loop()` + `gateway/notify.py` 新场景
`account_pool_ready`：5 分钟一轮统计可用账号数（停用/风控/冷却叠加后的结论），
只在「上一轮 >0 且本轮 =0」下降沿推送告警 —— 账号池归零是全网停摆级事件；
恢复不推送，连续归零由 send_scene 内容指纹去重。设置页可按场景开关。

## 修复

### 流式首帧 TTFB 埋点接线（优化7）

`reqlog.set_status()` 早已存在却一直没有调用方 —— 面板请求指标里首字节耗时
（模型开始吐字的延迟）一直是空的。流式路径首帧到达即记 `status + ttfb_ms`。

### refresh_guard 落盘加锁（优化7）

`_save()` 此前无锁，并发时两个线程同时 `os.replace` 同一 tmp 路径可把状态
文件写撕裂。整体收进 `_LOCK`。

### model_health autoEnableAccounts 进配置白名单（优化6 补字段）

此前不在 `_coerce_config` 键表，`config.get("autoEnableAccounts", True)` 永远
拿 True —— 用户关不掉，隐性常开。补进 `default_config` / `_coerce_config` /
`merge_config` 三处；使用点缺键默认改 **False**（旧配置文件不再隐性开启）。

## 变更

### 签到成功推送补「今日新增」「今日消耗」（院长需求）

`build_checkin_done()` 新增可选参数 `today_task` / `usage_today`：今日新增 =
签到 + 任务（台账口径）；今日消耗取自 WebUI 代理 `/api/credits/stats`（
`summary.usageToday` 已校准）。⚠️ 与补丁原版差异：原版直连官方 57890（口径
未校准、端口写死），本版走代理校准口径，端口跟随 `PORT`；取不到显示「—」。

## 判定记录（其余 2 项补丁）

- **wb_daily 刷屏根因修复** —— 上游已重构并入（只按 rc 判定），不重复。
- **anchor_pilot 锚点轮** —— 补丁无任何 API/UI 接线，未完成的实验代码，忽略。

## 测试

新增 `tests/test_merged_patches.py`（17 项）逐补丁锁定；
`test_model_health.py` +3 项（缺键不隐性开启）；`test_broadcast_analysis.py`
场景数 10 → 11。
