> 新增 Telegram 通知通道（含**渠道级独立代理**），并如实更正此前「与 MoviePilot 对齐」的不准确表述。

## 先更正两处我此前说错的话

1. **「其他用户仅配置几个字段就行」——不准确。**
   通知只要填字段即可；但**交互**（回调 + 菜单）多一个硬门槛：必须有**公网入口**
   （企微回调要能访问到 AutoBuddy）。没公网就只能收通知、用不了交互。

2. **「与 MoviePilot 对齐」——不准确。** 核实 MoviePilot 源码后，它的企微与我不是同一条路：
   - `wechat.py`（应用消息）：字段仅 `CORPID / SECRET / AGENTID / PROXY`，**只发不收，无回调**；
   - `wechatbot.py`（机器人）：走 **WebSocket**（`wss://openws.work.weixin.qq.com`），非 HTTP 回调。

   我实现的是企微官方标准的**自建应用接收消息服务器**（HTTP 回调 + 自定义菜单），
   是另一种合法方式，**并不等于对齐 MoviePilot**。
   同理，Telegram 的「渠道级代理」也比 MoviePilot 的「全局 PROXY 一份」更细，属扩展。

## 新增：Telegram 通道

- 注册 `telegram` 渠道：`bot_token`（secret）/ `chat_id` / `proxy`；
- 前端按注册表自动渲染，无需改前端代码；
- **渠道级代理**：只作用于 Telegram 通道的通知与交互，
  **不写入全局设置、不改变网关出网行为**（与 MoviePilot 的全局 PROXY 不同）；
- 新增 `POST /api/notify/probe`：**不发消息**的连通性自检（Telegram 走 `getMe` 验 Token）；
- `send_test` 仍不要求总开关，填完表单即可验证。

## 测试

新增 `_test_telegram.py`（17 项）：注册表字段、secret 脱敏（明文不泄漏）、
代理隔离性源码契约（只传该请求、不写全局）、空配置健壮性、不影响既有企微。
全量 31 个测试文件全绿；Python 语法 + 内嵌 JS `node --check` 通过。

## 升级说明

重建容器后在设置页「通知管理 → Telegram」填入 Bot Token / Chat ID，
代理按需填（留空=直连）。点「测试」即可实发验证。
