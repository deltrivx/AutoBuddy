# AutoBuddy v0.5.7

> 底层浏览器驱动升级为 Patchright（免检测自动化），保持动态下载机制与前端 UI 体验。

## 变更

### 1. 迁移至 Patchright 免检测驱动
- 采用 Patchright 替换标准 Playwright，消除 `navigator.webdriver` 等核心指纹特征，提升 GitHub 与风控页面的穿透能力。
- 代码导入无缝对齐 `from patchright.async_api import async_playwright`。

### 2. 保持动态下载与挂载持久化
- 镜像不内置大型浏览器，保持轻量。
- 启动/后台拉取执行 `patchright install chromium`，内核持久化存放于 `/data/.autobuddy/browsers`。
