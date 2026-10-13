> 修复 Telegram 交互命令路由：英文命令未被识别、accounts/models 缺分支、checkin 错跑全量任务。

## 缺陷（2026-10-13 端到端实测）

发布 v0.9.53 后实测 Telegram 交互，三条命令表现异常：

| 命令 | 实际回复 | 期望 |
|---|---|---|
| `/status` | 收到：status。发「帮助」查看… | 网关状态 |
| `/accounts` | 收到：账号。发「帮助」查看… | 账号余额清单 |
| `/checkin` | 已启动一轮每日任务… | 只签到 |

三个根因：

1. **英文命令未归一化**：`_wecom_interpret` 只认中文关键词（「状态」「积分」），
   而 Telegram 命令菜单用的正是英文（status / credits）。于是 `/status`
   落进「闲聊兜底」分支。
2. **accounts / models 无分支**：命令表里注册了，但解释器里根本没有对应处理，
   同样回落兜底。
3. **checkin 与 run 合并**：此前「签到」和「任务」共用一个分支、都打
   `/api/wb-daily/run`（全量每日任务），导致 `/checkin` 错跑成全量任务。

## 修复

- **命令开头归一化**：去掉 `/` 前缀与 `@bot` 后缀，英文命令映射为中文同义词后
  走同一套分支（status→状态、credits→积分、accounts→账号、models→模型、
  daily/run→任务、checkin→签到、start→help）。
- **补齐 accounts / models**：accounts 读 `/api/credits/stats` 出账号余额清单；
  models 读 `/health` 出模型总数 + 自动发现数。
- **拆开签到与跑任务**：`/checkin` 走 `/api/checkin/all`（只签到）；
  `/run`、`/daily` 走 `/api/wb-daily/run`（全量任务）。

## 验证

本地验证 8 条命令全部命中各自分支，不再回落「收到：xxx」兜底：

```
status     -> 状态读取失败，请稍后再试。        （命中状态分支）
credits    -> 暂无积分快照…                    （命中积分分支）
accounts   -> 账号获取失败…                    （命中账号分支）
models     -> 模型获取失败…                    （命中模型分支）
checkin    -> 签到提交失败…                    （命中签到分支）
run        -> 任务启动失败…                    （命中任务分支）
help /start-> AutoBuddy 指令：                 （命中帮助分支）
```

（此处"失败"仅是本地沙箱访问不到 18090/18091；容器内可正常取值，v0.9.53 已实测
credits 返回真实数值。）

## 测试

全量 32 个测试文件全绿；Python 语法 + 内嵌 JS `node --check` 通过。
