# v0.3.11 - 回归修复 + 范围收窄：彻底移除 CLI

> 本版起于一个现象：**每个账号前面又出现了三个桌面图标**。
> 排查后发现，真正的问题不是「图标回来了」，而是**整个补丁系统早就静默失效了**。

## 1. 根因：补丁全失效，而且失败被吞掉了

官方 `wb-switch` 已从 `0.1.36` 升到 **`0.1.40`**。这次发版把压缩产物里的 JSX 变量名从
**`m.jsx` / `m.jsxs` 改成了 `p.jsx` / `p.jsxs`**（后端端点常量也从 `Db` 变成 `zb`）。

而补丁脚本的 **13 条锚点全部把 `m.jsx` 写死**，于是**每一条都失配**。更糟的是：

- 锚点失配时脚本只打印一行 `- skip ...` 就继续；
- Dockerfile 又用 `... 2>/dev/null || true` 把失败吞掉。

结果就是：**补丁一个字节都没改，镜像构建却照样 success。**

实证——线上容器内的二进制 md5 与官方原始 `0.1.40` **完全相同**：

```
8e1dd723ca74e2a8405bc8be191abec5   /usr/lib/node_modules/workbuddy-switch/bin/wb-switch-linux-x64
8e1dd723ca74e2a8405bc8be191abec5   （官方 pristine 0.1.40）
```

所以三个桌面图标、footer、导入本机账号、权限检测、桌面状态图标全部复活了。

## 2. 补丁脚本重写：锚点自适应 + 失配即失败

- **锚点不再依赖压缩变量名**：一律用稳定的**文案 / `className` / `id`** 定位，再回溯到 JSX 调用起点，
  变量名从匹配结果里现取。新增的 `_call_start_before()` 会做**真正的包含性判断**——因为 marker 经常落在
  `children:[a?X.jsx(..):Y.jsx(..), …]` 这种三元里，简单取「最近的 `.jsx(` 匹配」会命中**兄弟**调用而不是**包裹**调用。
- **锚点失配 = 构建失败**：脚本收集所有失败项并打印明细，末尾以非零码退出；Dockerfile 改用 `set -eux`，
  **彻底删掉 `|| true` 与 `2>/dev/null`**。
- **锁定上游版本**：`workbuddy-switch@latest` → `ARG WB_SWITCH_VERSION=0.1.40`。

## 3. 移除清单：只留「自动签到」与「提供 API」

在 0.1.40 的新结构下逐条重新校准，现在被物理移除的是：

| 移除对象 | 原因 |
| :--- | :--- |
| 账号卡片头部三个图标 | WorkBuddy / CodeBuddy IDE / CodeBuddy CLI，点击都会调用宿主桌面程序 |
| 账号卡片底部三个产品槽位 | 同上 |
| 「当前账号」徽章组件 | 同上 |
| 桌面程序运行状态图标组 | 容器内无宿主客户端，状态恒为「未运行 / 未安装」 |
| 导入本机账号按钮 | 依赖宿主机客户端文件 |
| 设置页 **权限检测** | 检测 macOS 完全磁盘访问，容器里不存在该能力 |
| 设置页 **启动设置** | 「开机时静默启动到托盘」，纯桌面登录项 / 托盘概念 |
| 设置页 **自动更新** | 容器里升级靠镜像重建，自更新只会写进易失层 |
| 设置页 **限额监听** | 依赖「扫描 CodeBuddy IDE 日志」与「向桌面客户端装 hook」 |
| 设置页 **CodeBuddy CLI 自动轮换** | 随 CLI 一并移除 |
| Token 统计页数据来源 Tab | 容器内没有 IDE / CLI 数据来源 |
| CLI 专属「查看请求明细」按钮 | CLI 已移除 |

**保留**：`外观`（浅色/深色主题，WebUI 里真实可用）与 `自动签到`（核心功能）。

设置页总描述也同步改写：`自动签到、限额监听、权限检测与自动更新配置。`
→ `自动签到与账号保活，对外提供 OpenAI 兼容接口。`

## 4. 彻底移除 CodeBuddy CLI（不是隐藏）

上一版刚把 CLI 装进容器，这一版按「功能只集中到两点」的要求整体撤掉：

- **Dockerfile**：删掉 `npm i -g @tencent-ai/codebuddy-code`、`DISABLE_AUTOUPDATER`、
  `mkdir -p /workspace`、system 级 git 身份，以及只为 CLI 装的 `git`。
- **删除 `gateway/cli_bootstrap.py`**，以及 `entrypoint.sh` 里的 CLI 接入步骤。
- **`gateway/web_proxy.py`**：删除 `GET /api/cli-info`、`.wb-cli-scope-note` / `.wb-cli-status*` /
  `.wb-pool-bound` 三组样式、`injectCliScopeNote()`、`官方 CLI 绑定` 标记，以及 `/api/account-pool`
  里并入的 CLI 字段；连带的 `shutil` / `subprocess` / `time` 三个 import 也一并清掉。
- **Unraid 模板**：删掉 `/workspace` 挂载项，只留 `18090` / `18091` / `/data`。

## 5. 升级须知

- 数据卷不变，仍然是 `/data`（`/mnt/user/appdata/workbuddy-switch`）。
- 如果你之前按 v0.3.10 的说明挂载了 `/workspace`，现在可以删掉这个挂载项了。
- 镜像体积随 CLI 与 git 的移除回落。
