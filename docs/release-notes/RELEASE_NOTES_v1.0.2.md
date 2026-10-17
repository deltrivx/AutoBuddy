> 修复移动端「账号管理」误跳数据总览，并根治日报「昨日签到积分」「昨日调用」恒为 0。

## 一、背景

用户连报三个体验问题：

1. 移动端点「账号管理」跳到了「数据总览」。
2. 每日报告「昨日签到积分：0」（实际有签到奖励）。
3. 每日报告「昨日调用：0 次」（但累计调用正常，SQLite 明细里数据一直都在）。

前两个是实打实的逻辑 bug，第三个是跨天瞬间的时序抖动。本版一次性修复。

## 二、移动端「账号管理」误跳数据总览

顶栏收纳按钮（汉堡菜单 `#wb-nav-toggler`）展开的下拉面板 `rebuild()`，只复制了导航项的 `href` 与 `innerHTML`，并只绑了 `wbCloseNavPanel()`——**丢失了原始导航项上 `injectWbOverview` / `injectWbDaily` 绑定的 hash 路由语义**。

而「账号管理」的官方 `href` 是 `/`，与 v1.0.0 起作为默认主页的「数据总览」撞车，于是移动端点「账号管理」直接跳 `/` = 数据总览。

修复：在 `rebuild()` 里按导航项 id 复刻语义，先 `preventDefault()` 再收起面板：

| 导航项 | 跳转 |
| :--- | :--- |
| `WB_OVERVIEW_NAV_ID` | `wbPushUrl("/")` |
| `WB_ACCOUNTS_NAV_ID` | `wbPushUrl("/#/accounts")` |
| `wb-nav-wb-daily` | `wbPushUrl(pathname + "#/wb-daily")` |
| 官方路径项 | 保持默认 href 跳转 |

## 三、日报「昨日签到积分」恒为 0

`_collect_daily_report` 原本依赖 `web_proxy._merge_checkin_credits()` 补签到积分，而它读的是 WebUI 进程（18090）处理 `/api/checkin/status` 时才预热的**进程内内存缓存** `_CHECKIN_STATUS_CACHE`。

日报跑在网关进程（18091）的后台循环里，此处的 `web_proxy` 是另一个独立模块实例，缓存恒为空 → `_merge_checkin_credits` 直接 `return data` → 签到积分补不进去 → 恒 0。

修复：日报**直接**向官方接口 `http://127.0.0.1:57890/api/checkin/status` 取 `accounts[].raw.daily_credit` 与 `raw.checkin_dates`，自行累加昨日签到积分（只统计 `checkin_dates` 里含昨日、且 `daily_credit > 0` 的账号），并重算 `y_total = y_checkin + y_task`。

## 四、日报「昨日调用」跨天抖动报 0

该值走 `get_aggregated_token_stats()` 的 `sources[].daily[]` 聚合链，要经过三道关：

- `get_official_cloud_requests()`（官方底层 3 秒 timeout）
- `flush_pending(force=True)`（内存攒批落盘）
- variant 分流（cn / ai）

日报在 00:09 跨天瞬间生成时，任一环节抖动就会让「昨日」那天的记录暂时取不到，于是报成 0——而 SQLite 明细里 10-17 的 5162 条记录其实一直都在。

修复：新增 `token_store.count_calls_on_date(date)`，直接对 `usage_logs` 按 `date` 精确计数（明细每行 = 1 次调用），再叠加 `usage_rollup` 该天折算掉的 `records`，得到与聚合链 `records` 完全一致的口径，但不经过任何 timeout / 攒批 / 云端流水环节。日报里当聚合链取到 0 时，自动用这个精确计数兜底。

## 五、验证

- 全套 50 个 `_test_*.py` 测试文件全部通过（0 失败），尤其 `_test_version_sync.py`（版本号同步）、`_test_webui_panels.py`（移动端导航面板）、`_test_token_storage.py` / `_test_token_store_sqlite.py`（词元存储）。
- 容器内实测 `_collect_daily_report` 现在正确算出「昨日调用=5162」「累计调用=54785」。
