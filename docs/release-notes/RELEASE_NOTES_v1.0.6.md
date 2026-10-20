# v1.0.6

本次四笔改动：调用分流三档开关、API 密钥编辑与高危二次确认、401 卡片「需要重新登录」提示、配套测试。

## 新增

### 调用分流三档开关

账号池配置新增 `callRouting` 字段，三档：

- **all**（默认，行为与旧版完全一致）：全部账号参与自动轮询
- **cn**：自动轮询排除 AI 独占账号
- **ai**：仅 AI 独占账号参与自动轮询

显式指定账号不受档位限制（含 AI 独占模型的定向放行逻辑，该放行发生在分流过滤之前）。设置页「API 接入」卡片提供三按钮切换，当前档高亮；未提交档位时沿用原值，非法值返回 400。

### API 密钥编辑与统计重置

- 密钥行新增「编辑」：重命名密钥
- 「重置统计」：清零 callCount 与 lastUsedAt，密钥本身不变（新增 `POST /api-keys/reset-stats`，`web_proxy` 已转发）

## 安全

**密钥高危操作二次确认**：停用、删除、重置统计、清空全部四项高危操作统一加确认弹窗（停用原为直接执行）。密钥显示仍走 mask_key 脱敏。

## 修复

### 401 卡片不再显示一串错误代码

**根因**：账号凭据失效（401）时，官方积分包区请求积分接口拿到上游错误页，把 `<html><head><title>401 Authorization Required</title>…` 整段原始 HTML 当文本渲染在卡片里（挂在内文与 title 属性），位置在操作按钮上方。

**修复**：按错误页特征（`<html>` + 401 Authorization Required）识别该 DIV，整块替换为「⛔ 需要重新登录」+ 说明（凭据失效、积分用量暂不可读、重新登录后自动恢复）。挂载于注入链，MutationObserver 持续触发，React 重渲染的错误块也会被再次替换。**操作按钮、账号名、右上角标记完全不动；正常账号卡片零变化**（`wbCardNeedsRelogin` 守卫，正常账号偶发错误文本不误伤）。

### 需重登判定双来源合并

`/api/account-pool` 此前只看 refresh_guard 状态文件；accounts.json 里的 `needs_relogin` 标记（如官方禁用账号 Mr.H）被漏掉，前端因此毫无反应。现两路合并判定，并下发 `reloginReason`（来源 accounts.json 的 `needs_relogin_reason` 或 refresh_guard）。

## 测试

`_test_v106_features.py` 38 项断言覆盖：三档开关前后端行为与回归护栏（老配置无 callRouting 按 all 处理）、密钥编辑/重置/高危弹窗/脱敏、401 替换特征识别与正常账号守卫、后端双来源判定。全仓 52 个测试文件 ALL-GREEN。
