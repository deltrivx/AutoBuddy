> 企微推送的 `WECHAT_PROXY` 语义纠正为 **URL 重写**（此前误当 HTTP 代理），回调改为 **MP 模式**（立即返回 + 后台处理 + 主动推送），彻底解决 5 秒超时；智能助手从 17 个只读工具扩到 **30 个只读 + 10 个写操作**。

## 一、企微链路：根因与修复

### 问题

所有主动推送（日报 / 任务通知 / 助手答复）全部被企微拒绝：

```
errcode=60020 not allow to access from your ip
```

### 根因

两处叠加：

1. **代理语义理解错误**。MoviePilot 的 `WECHAT_PROXY` 不是 HTTP 代理，
   而是**替换 base_url**：

   ```python
   # MP 的 app/modules/wechat/wechat.py
   self._proxy = WECHAT_PROXY or "https://qyapi.weixin.qq.com"
   self._token_url = UrlUtils.adapt_request_url(self._proxy, self._token_url)
   ```

   `adapt_request_url` 内部用 `urljoin`。本版逐行对齐后，
   端点拼成 `http://107.172.90.165:8484/cgi-bin/message/send?...`。
   此前用 `httpx.Client(proxy=...)` 走 CONNECT 隧道，被转发器拒绝（501）。

2. **出口 IP 不在企微可信名单**。家里是双 WAN 家宽，出口在 NAT 池漂移，
   钉不住。VPS 固定 IP `107.172.90.165` 已加入应用 `1000012` 的可信 IP。

### 修复

- 新增 `_adapt_request_url()`，逐行对齐 MP 的 `UrlUtils.adapt_request_url`
- 渠道配置 `proxy` 语义改为 URL 重写（新增 `mode` / `admins` 字段）
- 恢复使用 AutoBuddy 自有应用凭据（AgentId `1000012`），不借用 MP 的应用

---

## 二、MP 模式回调：彻底解决 5 秒超时

### 参照依据

MoviePilot `app/application/messaging/ingress.py`：

```python
def submit_message_to_host(payload, source, *, submit, timeout=15):
    submit(forward_message_to_host, dict(payload), source, timeout=timeout)
    return True        # ← 立即返回，不等结果
```

线上日志实证（MP 助手 6 分 16 秒才回复）：

```
18:04:11.619  POST /api/v1/message/... 200 OK   ← 回调立即返回
18:04:11.626  收到微信消息：最近有哪些国内热门电影
18:04:17.281  执行工具 search_web（8694ms）
18:10:27.679  发送消息：根据搜索结果，以下是…
```

### 本版实现

```python
threading.Thread(target=_wecom_handle_async,
                 args=(cmd, from_user), daemon=True).start()
return Response(content="", media_type="text/plain")   # 立即返回
```

**回调响应与实际答复完全解耦。** 实测回调 0.005 秒返回，
复杂问题后台跑 13 秒仍能正常送达。

### 连带移除的临时补丁

为绕过 5 秒限制而加的这些机制，MP 模式下不再需要：

- 本地直答前置（关键词匹配分不清复合语义，曾导致答非所问）
- 快路径探测 + `_FAST_REPLY_BUDGET`
- `assistant_reply()` 的 deadline 制

---

## 三、智能助手补全

### 工具：17 → 30 只读 + 6 → 10 写操作

| 功能域 | 新增工具 |
| :--- | :--- |
| 任务 | `get_task_runs` / `get_task_detail` / `get_task_accounts` / `get_task_catalog` / `get_task_config` / `get_task_health` / `get_task_items` |
| 账号 | `get_account_health` / `get_account_models` |
| 系统 | `get_token_stats` / `get_reqlog` / `get_notify_config` |
| 写操作 | `notify_test` / `notify_menu` / `account_audit` / `gateway_selftest` |

### 设置页（对齐 MP 智能助手设置）

新增注册表驱动的通用配置项：上下文档 Token 上限、温度、思考深度、
输出语言、User-Agent、啰嗦模式、联网搜索。

**联网搜索**接入 Tavily，密钥走环境变量注入（不回传前端、不落盘明文）。

### 工具边界（写入代码注释）

「不给助手工具」≠「禁用接口」。认证类 8 个、危险操作 4 个、
助手自身配置 4 个等共 20 个接口在**面板完全可用**，
只是不给 LLM tool 声明 —— 防的是提示注入（如「帮我改成密码 123456」）。

---

## 四、修复的缺陷

| 症状 | 根因 |
| :--- | :--- |
| 「任务跑完了吗」答「暂无记录」 | `_cmd_daily()` 读内存态 `jobs`，改读落库 `runs` |
| 「账号凭据还有多久过期」答成账号余额 | 数据源无 `exp` 字段 + 路由被「账号」抢先 |
| `get_request_stats` 只输出光杆标题 | 未读 `summary` 子对象 |
| `get_token_stats` 输出原始列表 | `requests`/`sessions` 是列表，未取长度 |
| 企微注册表 `proxy` 字段重复 | 清理重复定义 |

---

## 升级说明

```bash
docker compose pull
docker compose up -d
```

新增环境变量（可选，不配则助手不启用联网搜索）：

```yaml
TAVILY_API_KEY: "${TAVILY_API_KEY:-}"
```
