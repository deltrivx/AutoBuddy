> 账号卡片三处交互缺陷修复（开关抖动 / 按钮跑位 / 移动端整页刷新），vendor 脚本升级 v3.14（含 500 积分补偿领取）。

## 一、更新摘要

本版（v1.0.5）聚焦前端注入层（`web_proxy.py`）的账号卡片交互体验，并升级官方引擎配套脚本以支持本次 500 积分补偿活动：

1. **开关不再整页抖动**：toggle / 设为首选 / 保存池配置成功后，不再「拆掉全部账号条 → 异步重新拉取 → 全量重注入」，改为按 `accountId` 定位对应卡片，同帧原子替换该卡片的账号条（`wbReplacePoolBar`），避免空窗与大批 DOM 变更触发整页重排。
2. **按钮不再跑位**：重建的账号条通过 `insertBefore + remove` 原子替换原位节点，不再 append 到 section 末尾与 React 异步内容竞争位置。
3. **移动端不再整页刷新**：注入的 24 处 `<button>` 全部显式 `type="button"`，不再因落在官方 `<form>` 内被当作 submit 触发整页刷新。
4. **vendor 脚本 v3.7 → v3.14**：`gateway/vendor/workbuddy_daily.py` 升级，新增 500 积分补偿逻辑（`compensation-status` 查询 + `claim-compensation` 领取 + 领取后回查确认到账，不以请求成败为准）。

## 二、技术细节

- 新增单卡片构建器 `wbBuildPoolBar(acc, data, cardEl)`，首次注入 / toggle / savePool / pin 四条路径统一复用，杜绝多份构建逻辑漂移。
- 新增就地重绘工具 `wbReplacePoolBar(accountId, status, cardEl)`：按 `article[data-wb-acc=...]` 定位卡片，原子替换账号条；找不到目标时兜底回退 `refreshPool()`。
- `wbToggleAccount` 的 `done(ok, status)` 回调透传后端响应中的最新 `status`，重绘用最新数据而非本地猜测。
- `wbEl()` 对 `tag === "button"` 统一设置 `el.type = "button"`；既有 14 处裸 `createElement("button")` 逐处补上 `.type = "button"`。
- 刷新入口 `refreshPool()` 保留，用于切页等场景的全量兜底。

## 三、配置与兼容性

- **零配置升级**：无新增配置项，无破坏性变更，升级容器即生效。
- **vendor 升级**：CLI 接口（`--no-desktop` / `--query` 等）与既有 `wb_daily.py` 调用方式完全兼容，无需改动网关侧调用代码。

## 四、升级方式

```bash
docker pull ghcr.io/deltrivx/autobuddy:latest
# Unraid：php /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/rebuild_container AutoBuddy
curl -s http://127.0.0.1:18091/health   # version 应为 1.0.5
```
