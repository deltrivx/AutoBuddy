# AutoBuddy v0.5.0

> 本次为**品牌更名版本**，功能与 v0.4.18 一致。

## 更名

项目更名为 **AutoBuddy**（原 WorkBuddy Switch）。更名覆盖范围：

| 位置 | 改前 | 改后 |
|---|---|---|
| 项目 / 镜像 / 容器名 | WorkBuddy Switch | **AutoBuddy** |
| 仓库 | `deltrivx/workbuddy-switch` | `deltrivx/autobuddy` |
| Unraid 模板 | `unraid/WorkBuddy-Switch.xml` | `unraid/autobuddy.xml` |
| WebUI 标题 / 抬头 | WorkBuddy Switch | **AutoBuddy** |
| 数据目录 | `/data/.wb-switch` | `/data/.autobuddy` |
| 环境变量前缀 | `WB_*` | `AB_*` |
| API 密钥前缀 | `sk-ab-` | `sk-ab-` |

> 底层仍是官方 `workbuddy-switch` npm 包，仅在其之上做容器化定制，
> 官方能力边界未做任何改动。

## 浏览器内核（承接 v0.4.18 修复）

- 下载进度结构化：百分比、已下载字节 / 总字节、当前阶段、已用时。
- 修复双进程同时下载抢 `__dirlock` 导致目录长期为空、进度卡死的问题：
  内核下载统一由注册服务 startup 钩子托管，`entrypoint.sh` 不再自行调用 playwright。
- 「账号接入」页面：未安装显示实时进度条并自动轮询；已安装显示持久化路径与内核目录。

## 升级注意

数据目录与环境变量前缀均已变更，从 v0.4.x 升级时：

1. Unraid 挂载改为 `/mnt/user/appdata/autobuddy/data` → `/data`；
2. 旧数据可从 `/mnt/user/appdata/workbuddy-switch/data/.wb-switch` 拷到新目录下并改名为 `.autobuddy`；
3. 自定义环境变量如设置过 `WB_VERSION` / `WB_DATA_DIR`，请同步改为 `AB_VERSION` / `AB_DATA_DIR`。
