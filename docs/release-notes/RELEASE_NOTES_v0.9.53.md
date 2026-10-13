> 照 MoviePilot 补齐 Telegram 交互：命令菜单自动注册 + getUpdates 长轮询（**不需要公网**）。

## 更正：Telegram 不需要公网

我此前把「交互需要公网入口」套到了 Telegram 上——**这是错的**。核实
MoviePilot（`app/modules/telegram/telegram.py`）后确认：它的 Telegram 收消息用
**`getUpdates` 长轮询**，不是 webhook，因此**不需要公网入口**。
只有企微的自建应用回调才需要公网。这点已在此版本更正并落实。

## 实现（照 MoviePilot，不自创）

### ① 命令菜单自动注册（同 `register_commands`）

`telegram_register_commands()`：先 `deleteMyCommands()`，再 `setMyCommands()`
注册 8 条命令（status / credits / accounts / models / daily / run / checkin / help）。
**配置保存即自动注册**，成员在 Telegram 输入框按「/」就能看到菜单 —— 与企微
底部菜单是同一套命令（`MENU_KEY_TO_CMD` 复用）。

### ② 长轮询收消息（同 `_run_polling`）

- `getUpdates`（timeout=30），串行处理、offset 递进确认；
- 支持 `/status`、`/status@bot` 两种写法，按钮回调 `callback_query` 也路由同一套命令；
- 失败按指数退避重试（上限 60s），单条处理抛错也确认 offset —— 避免同一条坏消息反复投递；
- 后台守护线程；容器启动即自动恢复（重启后不用手动点）。

### ③ 接线

- `PUT /api/notify` 保存配置后：就绪即自动注册命令菜单 + 启动长轮询，返回
  `telegram` 字段（含 `polling` 是否启动）；
- web_proxy 启动时调用 `start_telegram_polling()` 恢复轮询；
- 命令路由复用 `_wecom_interpret`，两通道行为一致。

## 关于「不是官方发送的请求」日志

该文案对应上游 **400 `code=11128`（unapproved channel 渠道拦截）**，v0.9.35 已加处理
（命中账号冷却 30 分钟）。用户说明**该日志来自其他设备、不在本容器**——本环境近 48h
实测 0 条，本地无从复现与修复，需在那台设备的上下文排查（不假装已解决）。

## 测试

新增 `_test_telegram_interact.py`（20 项）：命令名合法性、命令路由（复用企微同套）、
注册健壮性（缺 Token 不抛异常）、长轮询生命周期（无 Token 不启动 / 幂等 / 可停）、
源码契约（setMyCommands + deleteMyCommands + getUpdates，**不含 webhook**）。
全量 32 个测试文件全绿；Python 语法 + 内嵌 JS `node --check` 通过。

## 升级说明

重建容器即可。Telegram 填好 Bot Token / Chat ID（+ 可选代理）保存后，
命令菜单自动注册、长轮询自动开始；在 Telegram 里发 `/status` 或按「/」选命令即可得到回复。
