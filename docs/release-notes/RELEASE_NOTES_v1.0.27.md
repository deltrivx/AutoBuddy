## 修复

企微交互命令（账号余额、积分消耗、模型、状态、签到、任务、凭据）此前全部返回
「数据获取失败」。根因是 `gateway/notify.py` 的 `_http_json` 在调用
`/api/wb-daily/*`、`/api/account-pool`、`/api/gateway-info`、`/api/credits/stats`
这些挂在会话门后的接口时，没有携带任何会话凭据，服务端一律回 401。

本版本为 `_http_json` 增加内部服务会话：首次调用或遇 401/403 时，使用容器内
`AUTH_USERNAME` / `AUTH_PASSWORD` 环境变量登录并缓存 Cookie 复用。该会话仅在
容器内 loopback 使用，不对外暴露；登录失败后 5 秒内不重复尝试，避免失败风暴。

升级后无需额外配置，交互命令即可正常返回数据。
