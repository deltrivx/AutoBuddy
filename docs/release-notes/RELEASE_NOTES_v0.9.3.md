# AutoBuddy v0.9.3 发布说明

**发布日期**：2026-09-26

## 概述

本次发布**移除「账号接入」（GitHub 自动注册）功能**，并修复其在移除过程中造成的
每日任务面板与 Buddy 徽章适配函数缺失问题。

---

## 移除：「账号接入」功能全线摘除

该功能通过容器内 Chromium + playwright 自动注册 GitHub 账号，用于补充账号池。
实际使用率极低，却长期占用 **658MB** 磁盘（浏览器内核）与可观内存。

本次从容器中彻底摘除，覆盖后端服务、前端界面、启动脚本、环境变量与落盘数据：

| 部件 | 处理 |
| --- | --- |
| `gateway/gh_register.py` | 整个服务文件删除 |
| `/api/gh-register/*` 转发路由 | 删除（5733 字符） |
| 前端「账号接入」侧边栏入口与专属视图 | 删除（约 687 行） |
| `entrypoint.sh` 启动段与 `GH_PID` | 删除，启动编号顺延 |
| `docker-compose.yml` `GH_REGISTER_*` | 删除环境变量与代理说明 |
| `gateway/db.py` | 删除 `gh_register_config.json` 迁移逻辑 |
| 「每日任务」模块中的 3 处引用 | 解耦，改为直接锚定设置入口 |
| 挂载卷 `browsers/`（658MB） | 已清理 |
| 挂载卷 `gh_register_config.json` | 已清理 |

**注意**：本版本起，`GH_REGISTER_ENABLED` / `GH_REGISTER_PORT` 环境变量不再生效，
`PLAYWRIGHT_BROWSERS_PATH` 挂载点也不再被使用。

---

## 修复

### 每日任务面板与 Buddy 徽章适配函数被误删

**症状**：移除「账号接入」后，前端注入脚本整段不执行 —— 每日任务页面空白、
Buddy 徽章不显示飞机图标、侧边栏折叠按钮失效。

**根因**：删除时以文本行为边界，误将相邻的四个顶层函数一并清除：

- `wbCreateWbDailyView`（每日任务视图 HTML 构建）
- `wbUpdateWbDailyView`（每日任务视图切换）
- `injectWbDaily`（每日任务侧边栏入口注入）
- `wbBuddyBadgeKind`（Buddy 徽章状态判定）

而文件尾部 `DOMContentLoaded` 中仍在调用 `wbUpdateWbDailyView()`，
导致注入 JS 出现 `Unexpected token ')'` 语法错误，整段脚本不执行。

**修复**：回到干净基线，改用**模块边界整块切除**（按注释头精确配对）
而非逐函数文本删除，并按模块边界精确恢复被误删的函数。

### 新增回归防线

`_test_webui_panels.py` 现已对注入 JS 执行 `node --check` 语法校验 ——
注入脚本若存在任何语法错误，测试直接失败，防止同类问题再次逃逸到生产。

---

## 验证

```
node --check（注入 JS 语法）        ✅ 通过
Python AST（gateway 全模块）        ✅ 通过
13 个单测（含 _test_webui_panels）  ✅ 全部通过
版本一致性自检                      ✅ 通过
```

13 个关键前端函数逐个核对存在：每日任务三件套、Buddy 徽章两件套、
各注入器（账号模型 / 账号池 / API 接入 / 可用性巡检 / 账号资料 / 关于）。

---

## 升级说明

**无需任何手动操作**。重建容器即可：

1. 拉取 `v0.9.3` 镜像；
2. 按现有模板重建容器；
3. 旧的浏览器内核目录（658MB）不再被使用，可手动删除回收空间。

环境变量 `GH_REGISTER_ENABLED` / `GH_REGISTER_PORT` 可直接从模板中移除，
保留也不会报错（已无代码读取）。
