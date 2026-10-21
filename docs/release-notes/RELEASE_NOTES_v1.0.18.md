> 安全修复：网关本体（18091）32 个零鉴权路由补齐登录门，与 WebUI 同一套会话判定；账号池落盘前留时间戳备份。

## 安全

### 网关本体鉴权门（第二次同类事件）

**事件**：2026-10-21 线上账号池再次出现两条不存在的账号。v1.0.8 已给 WebUI（18090）的 54 个管理端点补过 `require_user` 门，但**网关本体（`gateway/main.py`，18091）一个都没覆盖** —— 而 18091 是 host 网络、`0.0.0.0` 监听（entrypoint 启动时不绑定地址），局域网内任何主机都能直连。

**根因**：`main.py` 37 个路由里 32 个零鉴权。`POST /import`（合并式写入）零校验，任何人 `POST http://<ip>:18091/import` 即可往账号池注入任意账号；`/delete`、`/export-accounts`、`/api-keys/*`、`PUT /account-pool/config`、`POST /rotate/run` 同样敞开。日志实证：`192.168.31.2` 在 03:47:23 `POST /import` 200、03:48:20 `POST /delete` 200。v1.0.14/15 新增的 `POST /account-pool/risk-flag/clear` 也只在 WebUI 侧加了门。

**修复**：

- `main.py` 新增 `get_current_user` / `require_user`，判定逻辑与 `web_proxy.require_user` 完全一致（读同一个 `db.validate_session`，未登录 401）。复制一份实现而非跨进程 import，是因为两个进程各自独立运行，模块状态不共享；共享的是判定标准。
- **30 个路由挂门**，逐个核对过用途；`/gateway/info` 虽只读，但响应含账号规模、密钥统计等侦察信息，一并挂门。
- **5 个端点刻意保留公开**（附代码注释说明理由）：
  - `GET /health` —— 容器健康检查 / Unraid 探活 / 部署后验证必须无凭据可达，加了门会把监控全打成 401、反而掩盖真实故障。响应只含版本号、模型计数等运行态摘要。
  - `GET/POST /api/notify/wecom/callback` —— 企业微信服务器固定回调入口，请求由外部平台发起，不可能携带本站会话；安全性来自 `cb_token` / `cb_aes_key` 签名校验。
  - `GET /v1/models`、`POST /v1/chat/completions`（含 `/models`、`/chat/completions` 别名）—— OpenAI 兼容 API，鉴权走 `api_keys`（`_gateway_auth`）那套，与会话门是两个体系；下游客户端拿不到浏览器会话 Cookie。

**不打断自家前端**：WebUI 代理转发到网关时，新增 `_gw_headers()` 把浏览器的 `autobuddy_session` Cookie 透传过去（26 个转发调用点逐个核对）。没有这一步，账号管理面板会在加门后全部 401 —— 那不是修好了安全，是把自家前端打断了。`/health` 与 `/v1/models` 的转发按设计不带头。

**回归测试**：`_test_gateway_auth_gate.py` 58 项断言，逐路由核对「白名单外一律挂门」+ 白名单本身不许混进高危写端点 + 转发透传 + 公开端点不被误挂，防回退。

## 变更

### 账号池落盘前留底

`_save_accounts()` 此前**不留任何痕迹** —— 这正是「账号被注入了但看不出是谁、什么时候改的」的直接原因（备份链最新一份停在事件发生前数小时）。现按仓内既有的 `backup_policy()` 思路补上：写入前把权威源（`/data/.wb-switch/accounts.json`）复制成 `accounts.json.bak-<UTC 时间戳>`，最多保留 20 份、超出删最旧（账号池最高频的写入来源是「刷新令牌」，一天可能几十次，全留会撑爆目录）。留底失败一律吞掉，绝不打断写入。只对权威源留底，容器副本是从同一份 payload 同步出来的，内容必然一致。
