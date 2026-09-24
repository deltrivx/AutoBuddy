# AutoBuddy v0.6.3

> 在 `/api/credits/stats` 的响应层加一段口径对账自检，降低同类故障的发现成本。

## 背景

v0.6.1 引入口径兜底、v0.6.2 补 `import time`，其实是同一件事的两面：

**反代层（`gateway/web_proxy.py`）自己 `return Response(...)` 的那些路由，没有 FastAPI 路由函数那层框架兜底。**
响应层一旦抛异常（如未定义名、序列化失败），前端拿到的不是 JSON 而是一段 HTML/错误页，
浏览器里只表现为一句 `The string did not match the expected pattern`，
真因（`NameError: name 'time' is not defined`）只留在容器日志里——排查只能靠 `docker logs` 翻栈。

## 变更

- 在 `_reconcile_official_usage()` 之后追加响应自检：设 `AB_DEBUG_RESPONSE=1` 时，
  把顶层 `summary.usageToday` 与 `officialUsage.summary.usageToday` 对照打一行 INFO 日志
  （`[credits] 口径对账 顶层=… officialUsage=…`）。
- 自检整体包在 `try/except` 内，**不得反过来打断主流程**——自检本身出错只记日志，不影响响应。

## 验收提醒

本版仍需按第 12 节纪律做**真实接口请求**验收：

```bash
docker exec AutoBuddy curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18090/api/credits/stats
docker exec AutoBuddy curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18090/api/account-models
```

期待两行都是 `200`。CI 的 11 个 `_test_*.py` 与 `py_compile` 都**拦不住运行期 NameError**，
所以「构建绿了」不等于「接口能打开」。
