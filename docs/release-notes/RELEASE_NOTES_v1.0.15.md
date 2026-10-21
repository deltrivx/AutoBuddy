# 修复解除风控标记按钮假成功

> 本版只修一个缺陷，是 v1.0.14 账号风控标记功能的收尾。若你正用 v1.0.14，
> 建议升级；徽章展示与推送通知本身在 v1.0.14 已正常工作，坏的只是「解除标记」按钮。

## 问题现象

在账号卡片的红色盾牌徽章浮层里点「解除这个标记」：

- 界面弹出提示「已解除风控标记」
- 接口返回 `200`、`{"ok": true}`
- 但刷新页面后，徽章**依然在那里**
- 服务端 `account_risk_flags.json` 文件内容**一个字节都没变**

前端一切正常，清除动作从未真正发生。

## 根因

本仓有两个服务进程：

| 进程 | 端口 | 职责 |
| :--- | :--- | :--- |
| `gateway/main.py` | 18091 | 网关本体，业务接口都在这里 |
| `gateway/web_proxy.py` | 18090 | Web UI，静态页面 + UI 专用接口 |

`risk-flag/clear` 端点只注册在了 `main.py`（18091）。而 UI 的 fetch 打的是同源的
`/api/account-pool/risk-flag/clear`，落在 18090。

`web_proxy.py` 末尾有一条兜底路由：

```python
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", ...])
async def proxy_all(request: Request, path: str):
```

它的职责是把「官方后端认识的路径」转给官方后端。任何没被显式注册的请求都会掉进这里。
于是这个 POST 被转给了官方后端 —— 官方不认识这个路径，却仍然返回 200。

**请求被静默吞掉，还伪装成成功。**

## 修法

在 `gateway/web_proxy.py` 显式注册该端点，转发到网关本体的同名接口：

```python
@app.post("/api/account-pool/risk-flag/clear")
async def account_pool_risk_flag_clear(request: Request, user: "str" = Depends(require_user)):
    ...
    r = await client.post(GATEWAY_BASE_URL + "/account-pool/risk-flag/clear", ...)
```

**不在 web_proxy 里另写一份落盘逻辑。** 风控标记的状态文件读写只保留
`gateway/risk_flag.py` 一处，web_proxy 纯转发 —— 两份实现必然漂移，那会引入更难查的问题。

## 给本仓的教训

往后在 UI 上加接口，**必须在 `web_proxy.py` 注册**，不能只写在 `main.py`。
否则会被 catch-all 吞掉，而且失败形态极其迷惑：前端显示成功，服务端毫无动静。

`_test_risk_flag.py` 已新增 4 项断言锁住这一点（端点字符串必须存在于 web_proxy 源码、
必须是转发而非本地实现、web_proxy 不得自行落盘、无停用调用面）。

## 升级说明

普通升级，无配置变更、无数据迁移。容器重建后 `/health` 应报 `1.0.15`。

## 未改动

- 徽章的展示逻辑、位置、配色 —— v1.0.14 已实测正确，本版不动
- 推送通知 scene `account_risk_flagged` 的内容与冷却 —— 不动
- 风控标记的存储格式与字段 —— 不动
- **只通知、不自动停用的红线** —— 不动，`autoDisableAccounts` 仍为关闭状态
