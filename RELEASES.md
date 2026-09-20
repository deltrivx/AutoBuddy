# WorkBuddy Switch Releases

[项目说明](README.md) | [更新日志](CHANGELOG.md)

本页是 **WorkBuddy Switch 的版本索引**。GitHub Releases 侧栏按发布时间排序，本页按语义化版本号排序，
避免后补的旧版本让版本顺序看起来错乱。

**当前稳定版：** [v0.4.6](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.4.6)

> 本文件由 `scripts/gen_releases.py` 从 `CHANGELOG.md` 生成，修改请改 CHANGELOG 后重新生成。

## 版本索引

| Version | 日期 | 更新摘要 | 发布说明 |
|---|---|---|---|
| [v0.4.6](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.4.6) | 2026-09-20 | 账号卡片里注入的两块固定收尾，「积分明细」不再一会儿在上一会儿在下 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.6.md) |
| [v0.4.5](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.4.5) | 2026-09-19 | 缓存命中率改为采用上游实测用量，版本号合并为一套，设置页「关于」重新排版 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.5.md) |
| [v0.4.4](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.4.4) | 2026-09-19 | 巡检探测彻底对齐真实调用，并补上「探测被拒」「凭据失效」两道跳过规则 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.4.md) |
| [v0.4.3](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.4.3) | 2026-09-19 | 修复巡检误报导致的成批误禁，禁用项区分「手动 / 巡检」来源，设置页补上「关于」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.3.md) |
| [v0.4.2](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.4.2) | 2026-09-19 | 模型可用性自动巡检：调不通自动禁用，恢复自动启用 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.2.md) |
| [v0.4.1](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.4.1) | 2026-09-19 | 模型标签可点击禁用，精确到「账号 × 模型」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.1.md) |
| [v0.4.0](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.4.0) | 2026-09-19 | 设置页新增 API 接入面板与访问密钥 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.0.md) |
| [v0.3.18](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.18) | 2026-09-19 | 修复账号调用次数随容器重建归零 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.18.md) |
| [v0.3.17](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.17) | 2026-09-19 | 清理数据卷里 CLI 时代遗留的痕迹 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.17.md) |
| [v0.3.16](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.16) | 2026-09-19 | 消除卡片上两个「调用次数」的口径混淆 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.16.md) |
| [v0.3.15](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.15) | 2026-09-19 | 更正 v0.3.14 的误判，并加改文案失效告警 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.15.md) |
| [v0.3.14](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.14) | 2026-09-19 | 修掉描述已移除产品的陈旧界面文案 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.14.md) |
| [v0.3.13](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.13) | 2026-09-19 | 修掉空状态里指向已删除按钮的误导文案 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.13.md) |
| [v0.3.12](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.12) | 2026-09-18 | 清掉最后两处 CLI 残留 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.12.md) |
| [v0.3.11](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.11) | 2026-09-18 | 彻底移除 CLI；修复补丁静默失效 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.11.md) |
| [v0.3.10](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.10) | 2026-09-18 | 补全容器内运行 CLI 的必要条件 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.10.md) |
| [v0.3.9](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.9) | 2026-09-18 | 让 CodeBuddy CLI 在容器里真正可用 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.9.md) |
| [v0.3.8](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.8) | 2026-09-18 | 全局巡检：修掉面板空数据与失真文案 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.8.md) |
| [v0.3.7](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.7) | 2026-09-18 | 新增并发分摊观测，并修掉一处并发隐患 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.7.md) |
| [v0.3.6](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.6) | 2026-09-18 | 补上卡片头部漏掉的桌面产品槽位 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.6.md) |
| [v0.3.5](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.5) | 2026-09-18 | 修复手动指定账号时状态不更新 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.5.md) |
| [v0.3.4](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.4) | 2026-09-18 | 卡片槽位物理切除 + 账号池并行调度 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.4.md) |
| [v0.3.3](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.3) | 2026-09-18 | 界面残留再清理，明细倒序与精简 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.3.md) |
| [v0.3.2](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.2) | 2026-09-18 | 卡片模型清单改为动态可用清单 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.2.md) |
| [v0.3.1](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.1) | 2026-09-18 | 请求明细从弹窗改为页面内平铺 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.1.md) |
| [v0.3.0](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.3.0) | 2026-09-18 | 界面源码级精简 + Token 统计引擎上线 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.0.md) |
| [v0.2.0](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.2.0) | 2026-09-18 | 容器化体验升级与全量模型网关支持 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.2.0.md) |
| [v0.1.2](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.1.2) | 2026-09-18 | 图标全链路对齐与 Sub2API 对接 | — |
| [v0.1.0](https://github.com/deltrivx/workbuddy-switch/releases/tag/v0.1.0) | 2026-09-18 | 初始版本发布 | — |

## 镜像标签

镜像由 GitHub Actions **云端构建**并推送至 GHCR（不在本地构建上传）：

```bash
docker pull ghcr.io/deltrivx/workbuddy-switch:latest   # 最新稳定版
docker pull ghcr.io/deltrivx/workbuddy-switch:0.4.6    # 锁定版本
```

## 部署产物

每个 Release 均附带以下部署产物：

| 产物 | 说明 |
|---|---|
| `WorkBuddy-Switch.xml` | Unraid 容器模板（**请以模板方式创建容器，勿手工拼接 `docker run`**） |
| `docker-compose.yml` | Docker Compose 部署文件 |
| `icon.png` | 512×512 容器图标（Unraid 模板使用） |
| `SHA256SUMS` | 上述产物的校验值 |
