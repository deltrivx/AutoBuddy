> 账号池白名单彻底收紧，负载均衡按 Token 平摊，每日任务安全分级 + 防风控加固，接入国际版每日活跃，全量账号独立硬件指纹。

## 安全

### 白名单彻底收紧：国际独占模型不再穿透全局白名单

**问题**：`_enabled_accounts` 收窄候选集时，国际独占模型（`_is_ai_exclusive_model`）会**从全池**挑 `ai` 账号做定向放行。结果是：用户在设置页把白名单收成只有国内账号，请求一个国际独占模型，网关仍然会把请求派给白名单之外的 `ai` 账号——白名单形同虚设。

**修复**：候选集一律先在 `enabledAccountIds` 白名单内生成；独占模型只在这张白名单候选集里进一步收窄为 `ai`。白名单内没有 `ai` 账号时候选集为空，不再越权放行（与 v1.0.4 起的「全禁用回退」策略保持一致：宁可让上游报真实错误，也不在网关层偷偷放行）。

### 每日任务安全分级与独立开关

- 任务目录 `TASK_CATALOG` 全部任务划分三档：`Normal` / `Medium` / `HighRisk`。
- 配置新增 `disabled_tasks` 持久化列表，前端支持**逐任务复选开关**，不再只能整组启停。
- 新增 `POST /api/wb-daily/run-task/{task_key}`：单任务手动触发，跑单项不必启动整轮。
- 前端进度看板展示 `已完成 / 总数` 比例（如 `0/总数`），面板重构支持按档位分组展示。

### 防风控加固

| 项 | 改动 |
| :--- | :--- |
| 429 退避 | `api_retry` 解析上游 `Retry-After`，与指数退避取更晚者，叠加随机 Jitter |
| 阻断恶性重试 | `t_accept_all` 逐个重试注入 1.5~3.5s 防抖退避，避免同刻并发放大请求 |
| 去已下线接口 | `t_compensation` 清理官方已下线的 `claim-compensation`（报 400），安全跳过 |
| 去脱机假桌面对话 | 剔除 `RichMeow_Chat` 降级事件上报，不再伪造桌面端活跃 |

## 新增

### 账号管理搜索过滤框

账号卡片上方、OAuth 扫码添加下方、四个功能按钮前方注入即时搜索框：匹配账号名称 / 手机号 / ID，输入即筛，并显示 `命中数 / 总数`。注入带两级兜底（找不到工具栏时插在卡片容器前；容器无父节点时插在容器内最前）， DOM 异常时安静退出——不再因为一个注入失败让整个设置页白屏。

### 大并发 / 超大 Token 请求平摊负载均衡

选号算法新增两层 Token 感知：

- **在飞 Token 跟踪**（`inflight_tokens`）与**近 120s 滑动窗口**（`recent_tokens`），按 8000 Token 分档桶参与排序——同一个账号不会同时被塞进多个巨型 Prompt。
- **粘性会话强制打散**：绑定账号已有在飞请求，或本请求估算 ≥ 4000 Token 时，解除粘性改派全池空闲账号。

配合既有的积分配额制（余额权重 + 时间衰减账本），单号高并发与超大 Prompt 压爆的情况得到控制。

### 国际版每日活跃接入

新增两个 vendor 脚本并落盘：

- `gateway/vendor/workbuddy_intl_daily.py`：国际版每日活跃。
- `gateway/vendor/workbuddy_intl_login.py`：国际版 OAuth 登录。

`wb_daily.py` 增加 `build_intl_account_store`，执行时按账号 `variant` 分流：`cn` 走国内版脚本，`ai` 走国际版脚本，两侧日志聚合后统一回报。

### 全量账号独立硬件指纹

新增 `gateway/fingerprint.py`：

- 基于 **RFC 4122 标准 UUIDv5** + 稳定 MAC 派生，为每个账号生成确定且独立的 `machineId`。
- 每个账号一份独立虚拟画像（平台配置、独立真实 UA），持久化到 `account_fingerprints.json`。
- `workbuddy_daily.py` 在桌面端与 Web 端上报中动态取用各账号自己的指纹，取代原先全池共用的公共桌面指纹。

## 变更

### 账号卡片双法则排序

- 法则一：正常账号优先，异常（需重登 / 被停用 / 被拦截）沉底。
- 法则二：同状态内按实时积分降序，积分高的靠前。

`/api/account-pool` 相应补充实时 `credits` / `balance` 字段。

## 修复

- **异常卡片模型区灰化**：需重登卡片的账号池控制条与模型标签区灰化禁用（`.wb-relogin-disabled`），恢复正常后自动还原。
- **移动端自适应**：调整账号卡片媒体查询样式，异常卡片提示块在窄屏完整显示，不再被裁切。
- **搜索框注入不再令设置页白屏**：`wbInjectAccountSearch` 原先在 `listParent.parentElement` 为空时直接抛异常，`run()` 中断导致后续所有面板（关于 / API 接入 / 巡检 / 助手 / 通知）一并消失。现改为两级兜底 + 找不到输入框安静退出。
- **测试断言随实现演进校准**（仅测试文件，未改生产代码）：
  - `_test_credit_weight_sched.py`：原断言 `ordered.sort(key=lambda a: int(inflight.get(...))` 出现 ≥2 次；实际正常路径已换成 `_concurrency_token_key` 多级排序，仅异常兜底保留纯 inflight 写法。
  - `_test_sticky_yield.py`：原正则写死 `if _sticky_should_yield(...)`；实际条件已扩展为 `bound_busy or is_large_token or _sticky_should_yield(...)`。
  - `_test_v105_ui_fix.py`：原断言 vendor 脚本含 `compensation-status` 探测与领取后回查；该接口已官方下线，改为断言「已下线接口被安全跳过、不再发起请求」。

## 升级说明

从 v1.0.8 升级无需额外操作：重建容器即可，数据目录 `/data/.autobuddy` 与 `/data/.wb-switch/` 原样保留。新增的 `account_fingerprints.json` 会在首次执行每日任务时自动生成；每日任务的 `disabled_tasks` 缺省为空列表，等价于此前「全部启用」的行为。
