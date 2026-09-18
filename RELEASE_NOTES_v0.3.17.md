# v0.3.17 — 彻底清除 `/data` 下的 CodeBuddy CLI 残留

CodeBuddy CLI 早在 v0.3.11 就从镜像里移除了，但它在**数据卷**里留下的痕迹一直没清。
本版做一次性清理，并修掉唯一还在「制造」残留的源头。

## 清除清单

删除前已打包备份到 `/mnt/user/appdata/workbuddy-switch/backup/`。

| 路径 | 体积 | 性质 |
| :--- | ---: | :--- |
| `/data/.codebuddy/` | 18 MB | CLI 家目录：`settings.json`（`apiKeyHelper` 指向 CLI helper）、插件市场缓存、`projects/` `sessions/` `traces/` `shell-snapshots/`、`local_storage/` |
| `/data/.codebuddy-rotate/` | 8 KB | CLI 的 `helper.cjs`（apiKeyHelper）+ `state.json` |
| `/data/.wb-switch/hook.sh` | 102 B | CLI 的 Stop / FinalStop 钩子脚本 |
| `/data/.wb-switch/hook-events.jsonl` | 865 B | 上述钩子的事件日志（`"client":"CLI"`） |
| `/data/.wb-switch/hook-backups/` | 178 B | CLI `settings.json` 的备份 |
| `/data/.npm/` | 2.8 MB | 当初 `npm i -g` 装 CLI 留下的 npm 缓存 |

合计约 **21 MB**。

## 源头修复

`/data/.wb-switch/rotate` 是个**每次容器启动都会被重建**的空目录 —— 光删文件治不了，必须改 entrypoint。

```diff
- mkdir -p /data/.wb-switch/rotate
+ # 只确保数据目录存在。不要再创建 /data/.wb-switch/rotate —— 那是 CodeBuddy CLI
+ # 时代的 token 轮换目录，CLI 已于 v0.3.11 彻底移除，空目录留着纯属残留。
+ mkdir -p /data/.wb-switch
```

这是本版**唯一的代码改动**，二进制补丁数仍为 19。

## 明确不动的两项

经核实**不属于** CLI 残留，故保留：

- `/data/.config/CodeBuddy`、`/data/.config/CodeBuddy CN` —— 属 **IDE** 集成，
  官方二进制至今仍在写 `/data/.wb-switch/codebuddy_ide.json`（实测持续更新）；
- `/data/.wb-switch/auto_rotate_config.json` / `auto_rotate_logs.json` ——
  官方二进制**自带的**账号轮换功能（Rust core `wb_switch_core::modules::rotate`，
  对应 `/api/rotate/config`），当前 `enabled: false`，与 CLI 无关。

## 验收

- 删除后重启容器，上述 6 项**均未被重建**；
- `/data` 只剩 `.config`（IDE）与 `.wb-switch`（实时数据）；
- 账号 7 个完好；`18090 /`、`/api/account-pool`、`/api/account-models`、`/api/token-stats`、
  `18091 /health`、`/v1/models` 全 200；日志 0 异常；`/v1/chat/completions` 冒烟正常；
- 响应层文案改写仍生效（`按项目`=0 / `按账号`>0）。

## 顺带查明（未改动）

官方前端里仍有一段 **CodeBuddy CLI 接入确认对话框**的代码（含 `~/.codebuddy-rotate/helper.cjs` 文案）。
经静态可达性分析确认它是**死代码**：

- 唯一的开启函数 `async function $n(){Lt(!0)}` 在全 bundle 内**零调用点**；
- 另一处 `Ot` 对话框的唯一设置者 `An` 只作为卡片 prop 传入，而卡片组件 `$X` 内部从不调用它。

浏览器实测四个页面 DOM 中 `CLI` / `CodeBuddy` / `IDE` / `helper.cjs` 命中数**均为 0**。
因不可达、用户不可见，本版不做二进制补丁。
