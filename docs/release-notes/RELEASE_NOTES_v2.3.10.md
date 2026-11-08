# AutoBuddy v2.3.10 发布说明

> 发布日期：2026-11-08
> 对比基准：[v2.3.9](https://github.com/deltrivx/AutoBuddy/compare/v2.3.9...v2.3.10)

## 智能路由页三项回归修复

### 1. 左侧智能体在 Unraid 生产环境不再坍缩为「OpenAI」

**现象**：v2.3.9 在 FNAS 测试容器能分出 OpenClaw / DeepSeek 等真实智能体，但 Unraid 正式生产容器左侧只会显示「OpenAI」。

**根因**：生产所有请求经统一网关转发，客户端不携带 `x-autobuddy-client`、user-agent 千篇一律为通用 SDK 探测串（`OpenAI/JS 7.28.0` 等）。v2.3.9 的 `_wbRtRawCaller` 在 `caller_name=="gateway-local"` 时优先回退 `client_tag`（即这个通用 UA），于是所有来源都被归一化成同一个「OpenAI」。测试容器能分离，是因为测试客户端显式带了有区分度的 `client_tag` / `x-autobuddy-client`。

**修复**：当 `client_tag` 命中通用 UA 正则（`openai` / `curl` / `python-requests` / `node` / `go-http` 等探测器）或仍是 `gateway-local` 时，回退到 **API Key 的真实可读身份**（`wbRtKeyName(api_key)`，`/api/api-keys` 的 `id→name` 映射支撑）。生产 5 个 API Key（DeltrivX / iMohican / Mr.H / Chkris / X）将各自显示为独立智能体节点。

### 2. 点击「设置 → 智能路由」链接修正为同级

**现象**：在系统设置页点「智能路由」会出现 `IP:18090/settings#/routing`，与系统设置嵌套而非同级。

**修复**：导航点击处理器改为 `wbPushUrl("/#/routing")`，与 accounts / overview 等注入视图一致，统一推到根路径 hash 视图，与系统设置并列同级。

### 3. 移动端供应商配置行与分组头风格统一

**现象**：标题、分组头居中，但配置行内标签仍左对齐，视觉违和且右侧留白。

**修复**：窄屏下 `.wb-rt-cfg-row` 改为 `flex-direction:column; align-items:center; text-align:center`，标签与分段控件整行居中堆叠，与同卡片 `.am-gh` 分组头风格统一。

## 验证方式

本次通过 **CDP（Chrome DevTools Protocol）注入待测 CSS + 几何探针** 对真实生产数据做了闭环验证（不再依赖截图肉眼验收）：

- 390px 视口下路由视图 **零横向溢出**（扫描全部 `#wb-routing-view` 子节点 `right ≤ 390`、无负 `left`）；
- 顶端标题/返回键、实时轨迹统计、配置行、分组头、账号行全部居中一致；
- 实时页面 `window.__wbRtKeyNames` 映射在线可用（`k3ea687ac→DeltrivX` 等），左侧智能体将按 API Key 身份正确分离。

## 升级指引

常规 `docker pull ghcr.io/deltrivx/autobuddy:latest` 后按模板重建即可；本版仅前端（web_proxy，18090）与路由逻辑变更，数据目录无结构变化。
