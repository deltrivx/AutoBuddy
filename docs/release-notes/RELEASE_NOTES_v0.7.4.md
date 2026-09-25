# AutoBuddy v0.7.4

> 设置页彻底移除自动签到、全局服务标识统一对齐为 AutoBuddy、彻底修复页面切换卡顿与高频渲染死循环。

## 1. 设置页彻底移除自动签到功能
- 按照统一收拢至「每日任务」的设计规范，在 `patch_binary.py` 中将官方设置页的 `settings-auto-checkin` 重新加入物理移除清单，彻底移除设置页的原生签到板块，避免两处重复配置产生歧义；
- 签到配置、定时调度与最近 30 天历史流水统一由「每日任务」专属面板全权负责。

## 2. 后台服务与系统标识统一对齐为 AutoBuddy
- 全盘排查并更新 Unraid 巡检脚本（`user.scripts/scripts/unraid-health-post-update-check`）中的服务探测项为 `AutoBuddy(网关)`；
- 更新 Unraid `docker.config.json` 模板映射为 `autobuddy`，容器内软链接支持 `/usr/local/bin/autobuddy`；
- 官方闭源底层 npm 包名（`workbuddy-switch`）作为底层依赖标识符保留以保证容器冷启动兼容性，其余展示与运维名称全量对齐为 AutoBuddy。

## 3. 彻底消除页面切换严重卡顿与高频渲染死循环
- **根因定位**：
  1. `wbCheckAuth()` 缺乏执行中状态锁与完成态守卫，被 MutationObserver 持续触发网络请求风暴（已积攒近万次 `/api/auth/status`）；
  2. `injectAccountConnect()` 与 `injectWbDaily()` 在每次全局 DOM 变更时均无条件同步触发子页面重新隐藏与全量渲染加载；
  3. `MutationObserver` 监听整个 document 树时缺乏高频防抖（Debounce），微小的节点移动即触发死循环雪崩。
- **修复**：
  - `wbCheckAuth()` 补齐 `__wbAuthChecking` 状态锁，认证检查完成后彻底静默；
  - 移除侧边栏 DOM 注入时的副作用刷新调用，路由切换统一交由 `hashchange` 事件精确监听驱动；
  - `MutationObserver` 全面引入 `requestAnimationFrame` 防抖机制，消除 DOM 级联重绘，页面切换恢复丝滑流畅。
