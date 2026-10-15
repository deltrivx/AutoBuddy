> 参考 MoviePilot 的 `wechatbot.py`，为 AutoBuddy 新增企业微信**智能机器人 WebSocket 通道**——出站长连接，不校验来源 IP，解决 HTTP 通道 `errcode=60020` 导致的通知全阻塞。

## 用户反馈

> 「我让你参考 MP 智能助手修复你自己，你跑去动 MP 智能助手？」
> 「我都说了很多次了，moviepilot 有完整的企业微信配置链路，让你参考实现」

## 问题

AutoBuddy 的企微通知走 HTTP `cgi-bin/message/send`，要求出口 IP 在企微后台
「管理工具 → 可信 IP」名单内。实测三条候选出口**全部**被拒：

| 通道 | 出口 IP | 结果 |
|---|---|---|
| 容器直连 | `112.194.50.154`（NAT 池，还见过 `.151`/`.1`） | ❌ 60020 |
| 内网代理 31.10:7890 | `18.140.235.192` | ❌ 60020 |
| VPS 固定 IP | `107.172.90.165` | ❌ 60020 |

⇒ 日报、任务通知、助手延后答复**全部**发不出去，用户侧表现为
「助手只回『正在查询』然后没下文」。

## 参考 MoviePilot 后的结论

读 MP `app/modules/wechat/wechatbot.py` 发现关键：它的「MP影视助手」用的是
**智能机器人 WebSocket 长连接**（`wss://openws.work.weixin.qq.com`）——
**出站连接，不校验来源 IP**。这正是不用维护可信 IP 名单的原因。

其 HTTP 应用消息通道（`wechat.py`）日志里反而明确失败
（`获取微信access_token失败`），说明 MP 实际主用的就是 WebSocket。

## 实施

新增 `gateway/wecom_bot.py`，协议对齐 MP 实测：

| 能力 | 实现 |
|---|---|
| 订阅 | `aibot_subscribe`（body: bot_id / secret） |
| 收消息 | `aibot_msg_callback`，提取 text / voice / mixed / quote，去 @提及，忽略群聊 |
| 发消息 | `aibot_send_msg`，markdown，单条 4000 字节分片（多字节安全） |
| 心跳 | `ping` 30s |
| 可靠性 | `req_id` ACK 配对（10s 超时）、重连退避 [1,2,5,10,30,60] |

接入：

- `notify.CHANNELS` 增加 `bot_id` / `bot_secret` / `bot_chat_id` 三字段
  （bot_secret 走 secret 语义：GET 不回明文、空串清除、不提交保留）
- `_send_wecom()` **bot 优先，HTTP 兜底**——配了机器人就走长连接，
  未配则回落原 HTTP 通道（行为不变）
- `public_view()` 的 ready 判定纳入 bot 通道（它才是当前能打通的主通道）
- `web_proxy` 启动即恢复长连接（同 Telegram 长轮询）
- Dockerfile 增加 `websocket-client`

## ⚠️ 需要你配置

在企微后台 → 应用管理 → 对应的**智能机器人**页，取 `bot_id` 与 `secret`，
填到设置页 → 通知管理 → 企业微信的「机器人 ID / 机器人 Secret」。
（与自建应用的 CorPID/Secret **不是**同一组。）

## 测试

新增 `_test_wecom_bot.py`（**43 项**）：

- 模块契约：WS 地址、四类 cmd、心跳/ACK 时限、分片上限、重连表、
  UTF-8 续字节安全、群聊忽略、voice/mixed/quote 提取、前缀归一、启动幂等；
- 渠道接入：三字段、secret 标记、bot 优先 + HTTP 兜底、ready 判定、
  启动函数、消息走统一命令路由；
- 接线：web_proxy 启动钩子、Dockerfile 依赖；
- **行为验证**：超长中文分片不切断字符、空内容、markdown 拼接（真换行）、
  chatid 前缀归一、凭据为空不启动、恰好等于上限不拆片。

过程中测试抓出并修掉两个真 bug：写入时 `\\n` 双重转义导致分片/markdown
里是字面量 `\n`；`@\\S+` 双重转义导致 @提及根本没去掉。

同步更新 `_test_notify.py` 字段枚举（加入 bot 三件套）。

全量 **47 个测试文件全绿**；Python 语法 + 内嵌 JS `node --check` 通过。

## 升级说明

重建容器即可。`/data` 数据不受影响；未配置 bot 凭据时行为与之前完全一致
（仍走 HTTP 通道）。
