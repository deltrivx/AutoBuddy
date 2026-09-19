## 需求

CodeBuddy CLI 早在 v0.3.11 就从镜像里移除了，但它在**数据卷**里留下的痕迹一直没清。
本版做一次性清理，并修掉唯一还在「制造」残留的源头。

## 清除清单

删除前已打包备份到 `/mnt/user/appdata/workbuddy-switch/backup/`。

| 路径 | 体积 | 性质 |
| :--- | ---: | :--- |
| `/data/.codebuddy/` | 18 MB | CLI 家目录：设置文件、插件市场缓存、`projects/` `sessions/` `traces/` `shell-snapshots/`、`local_storage/` |
| `/data/.codebuddy-rotate/` | 8 KB | CLI 的凭证 helper 与状态文件 |
| `/data/.wb-switch/hook.sh` | 102 B | CLI 的 Stop / FinalStop 钩子脚本 |
| `/data/.wb-switch/hook-events.jsonl` | 865 B | 上述钩子的事件日志 |
| `/data/.wb-switch/hook-backups/` | 178 B | CLI 设置文件的备份 |
| `/data/.npm/` | 2.8 MB | 当初全局安装 CLI 留下的 npm 缓存 |

合计约 **21 MB**。

## 源头修复

`/data/.wb-switch/rotate` 是个**每次容器启动都会被重建**的空目录 —— 光删文件治不了，
必须改启动脚本。现在不再创建它。

这是本版**唯一的代码改动**。

## 明确不动

经核实，以下两项**不属于** CLI 残留，保持原样：

- `/data/.config/CodeBuddy`、`/data/.config/CodeBuddy CN` —— 属 **IDE** 集成，
  官方程序至今仍在写入与更新；
- `/data/.wb-switch/auto_rotate_config.json` / `auto_rotate_logs.json` ——
  官方程序**自带的**账号轮换功能（当前未启用），与 CLI 无关。

## 验收

- 删除后重启容器，上述 6 项**均未被重建**（`rotate` 需等本版镜像上线后才消失）；
- `/data` 只剩 `.config`（IDE）与 `.wb-switch`（实时数据）；
- 账号数据完好，控制台与网关各接口全部正常，日志无异常。

## 升级方式

```bash
docker pull ghcr.io/deltrivx/workbuddy-switch:v0.3.17
docker pull ghcr.io/deltrivx/workbuddy-switch:latest
```

> Unraid 请通过容器模板重建，**不要手工拼接 `docker run`**。
> 数据目录不变（`/data`），升级不会动到账号数据。

## 不改变的行为

账号管理、自动签到、账号池调度与 Token 统计全部保持不变。
本版只做数据卷清理与启动脚本的一处修正，界面与接口无变化。
