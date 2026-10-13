> 设置页新增「通知管理」：多渠道通知框架（参照 MoviePilot），首渠道企业微信。

## 背景

用户需求：参照 MoviePilot 的通知配置，在设置页添加通知管理；先做企业微信渠道的
框架与前端 UI，账号由用户稍后填入再测连通。

## 实现

### 框架（gateway/notify.py，新模块）

- **渠道注册表驱动**：`CHANNELS` 声明渠道与字段（前端按注册表自动渲染表单）；
  后续加 Telegram / 钉钉 / Bark 只需注册 + 实现 sender，前后端自动带上。
- **配置持久化** `notify_config.json`（原子写，mtime 缓存，与 model_policy.json 同风格）。
- **凭据安全**：secret 不回传前端（GET 只回 `has_value` 布尔）；保存语义
  「空串=清除、不提交=保留」；上游错误信息抹掉 access_token 再返回。
- **API**：`GET/PUT /api/notify` + `POST /api/notify/test`
  （测试发送不要求总开关——填完表单立刻验证连通性）。

### 企业微信渠道（对齐 MoviePilot 的 app/modules/wechat/wechat.py）

- 三要素：CorPID + 应用 Secret + AgentId（可选 touser，缺省 @all）；
- 流程：`gettoken(corpid+corpsecret)` → `message/send(agentid, text)`；
- 默认域 `qyapi.weixin.qq.com`；AgentId 非数字给友好报错；
- 配齐三要素才算「已就绪」，面板显示就绪状态。

### 前端（设置页注入卡片「通知管理」）

- 总开关（启用通知）+ 各渠道表单（注册表驱动渲染）；
- 每渠道「保存」+「测试」按钮；测试先保存当前表单再真发一条；
- secret 字段已配置时显示「已配置，留空保持不变」占位；
- 注入模式复用现有 wbFindSettingsHost / wbEl / wbApiAction 助手，
  与「模型可用性巡检」卡片同款交互。

## 测试

新增 `_test_notify.py`（25 项）：注册表契约、secret 脱敏与保存语义、
测试发送门禁、token 抹除、落盘一致性、企微流程源码契约（对齐 MoviePilot）。
全量 28 个测试文件全绿；Python 语法 + 内嵌 JS `node --check` 通过。

## 升级说明

重建容器即可。设置页会出现「通知管理」卡片；填入企业微信三要素后点「测试」
即可验证连通（无需开总开关）。事件推送（每日任务完成等）将在后续版本接入。
