> 新增请求级指标（脱敏）、成长任务等待异步计分落定后补领奖，并标注在线配置的热/冷边界。

## 一、请求级指标（新增）

参照 workbuddy2api-panel 的 `internal/reqlog`，新增 `gateway/reqlog.py`：只归档**请求元数据**，不写提示词、响应正文、Authorization 或其它凭证。

**设计取舍：**

- 有界环形缓冲（默认 200 条，`AB_REQLOG_RECENT` 可调）+ 进程级累计计数，不会随时间无限增长；
- **流式响应在流结束后才记 `total_ms`**，首字节耗时单独记 `ttfb_ms`。否则流式请求的耗时会严重低估（TTFB 只有 1~2s，整条流可能几十秒），指标失真；
- 只埋点 `/chat/completions`，面板轮询接口（`/api/*`）若也埋点会把指标刷爆。

**对外两端点：**

| 端口 | 端点 | 作用 |
| :--- | :--- | :--- |
| 18091 | `GET /reqlog` | 网关侧快照（成功率 / 耗时分位 / 最近请求） |
| 18090 | `GET /api/reqstats` | WebUI 转发到 `/reqlog` |

⚠️ **为什么必须走 HTTP 转发，不能让 web_proxy 直接 import**

指标是**进程内内存态**，只活在网关进程（18091）。WebUI 代理（18090）是另一个进程，直接 `import reqlog` 拿到的是另一个空实例，面板永远显示 0 条，而代码、`py_compile`、全套测试全绿，看不出任何问题。与 `token_tracker` 走 SQLite 可跨进程读不同，内存态指标必须经 HTTP 取。

## 二、成长任务：等异步计分落定后补领奖（修复）

vendor 脚本在 `run_account` 末尾一次性领走当时已 completed 的任务。但上游进度是**异步累加**的：脚本跑完时仍是 `accepted` / `in_progress` 的任务可能几秒后才达标，而脚本已经退出，这些任务**永远不会被领**。

新增 `_wait_and_claim_pending()`，放在 `gateway/wb_daily.py` **服务层**（不改 vendor 脚本，遵守 `vendor/README.md` 的「不做任何修改」）：vendor 子进程返回后，对脚本退出时仍未达标的任务码最多 5 轮 × 2s 轮询，达标即补领。

**两个护栏：**

- vendor 领过的都已 `claimed`，不会重复领；
- **AT 来源必须有优先级**（实测踩点）：vendor 运行期会 refresh 出新 AT 并写回 `WORK_DIR/wb_refresh_tokens.json`，而服务层 `store` 里仍是脚本启动前的旧 AT，直接用旧 AT 会 401。故优先读刷新后的 token 文件，读不到才回退旧 AT。

整体约 10 秒，任何异常都不影响主流程（只记日志）。

## 三、在线配置：明确热 / 冷边界（变更）

实测澄清两件事：

- **面板配置本来就是热的**：`_scheduler_loop()` 每 5 分钟重读 `load_config()`，`_execute()` 每次重新读 —— 改 `enabled` / `interval_hours` 下一轮即按新值走，不需重启。不要为此做 livecfg 过度设计（曾误判为「缺热生效」，实际已有）。
- **env 常量是冷的**：模块导入时读一次（实测改 env 后 `SOFT_RATE_BASE` 不变），必须重启容器才生效。

`GET /api/wb-daily/health` 新增 `config_scope`，把两组分开返回：`hot`（面板项，由 `DEFAULT_CONFIG` 派生）与 `cold_env`（env 常量名）。目的是消除「改了 env 却期待立即生效」这类误报：先核对改的是面板项还是 env 常量，别急着改调度逻辑。

## 测试

```
全量 21 个测试文件     ✅ 全绿
_test_reqlog.py       ✅ 13 项（归档 / 流式耗时不被低估 / 有界缓冲 / reset）
_test_config_scope.py ✅ 11 项（热冷分组 / 补键写入）
```

## 升级说明

重建容器即可，`/data` 数据不受影响。新增可选环境变量：`AB_REQLOG_RECENT`（默认 200）、`AB_REQLOG_CLIENT`（默认 1）。
