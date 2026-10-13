> 修复「交互菜单还是没变」的根因：命令语义收拢为唯一实现，删除 web_proxy 里重复的内联命令链。

## 用户反馈

> 交互菜单还是没变，同时查看任务进度和算法结果是否符合预期

## 实测根因（这次真正找到了）

`gateway/web_proxy.py` 的企微回调处理器里**内联了一份** if/elif 命令链，
与 `gateway/notify.py` 的 `_wecom_interpret` 是**两份代码**。

关键证据：日志显示 `POST /api/notify/wecom/callback` 由 web_proxy（18090）服务，
所以回调实际走 web_proxy 那份内联链。

症状（实测复现）：

| 调用路径 | 返回 |
|---|---|
| 直接调 `notify._wecom_interpret("text", "accounts")` | 新口径 ✅ |
| 走 HTTP 回调端点 | 旧口径（23 个账号逐条列表）❌ |

⇒ 之前 v0.9.62 只改了 notify.py，**对回调完全无效**。这就是「菜单还是没变」的原因。

## 修复

命令语义收拢为**唯一实现** `notify.wecom_command_reply()`：

- `web_proxy` 回调只做加解密 + `run_in_threadpool` 委托（同步函数不阻塞事件循环）；
- `_wecom_interpret` 也改为纯委托（MenuKey → 文本 → 同一函数）；
- 删除 web_proxy 里约 5KB 的内联命令链。

## 口径（按你之前的需求）

- **账号余额** → 完整列出全部账号积分（不再只给顶部四项），并附合计
- **模型** → 「当前模型」+ 全量清单（分批列出）+ 轮检禁用/手动禁用分类
- 其余：积分消耗（顶部四项）、状态、签到、任务进度/启动、通知开关

## 测试

新增 `_test_command_routing.py`（28 项）：

- 唯一实现存在、口径函数齐全；
- **两个入口都委托**：web_proxy 无 accounts/credits/models/status/daily/run/checkin 分支、
  使用 `run_in_threadpool`、`_wecom_interpret` 委托且不再自己发 HTTP；
- 口径函数调用链（`/api/credits/stats`、`/api/account-models`、基址常量）；
- 真实模块导入 + 路由分发（help/兜底/英文归一化，不依赖网络）。

全量 **36 个测试文件全绿**。

## 升级说明

重建容器即可。`/data` 数据不受影响。
