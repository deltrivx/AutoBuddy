> 回答一个问题：**CodeBuddy CLI 助手在容器里有意义吗？**
> 有 —— 而且本版把它从「配置写好了却没人用」的空转状态，变成了容器内真正可执行的环境。

## 一句话总结

镜像内置 CodeBuddy CLI，容器启动时自动接好认证链路，`docker exec` 进去就能用 `codebuddy`，token 由账号池自动注入、**无需交互式登录**。

---

## 1. 先回答那个问题：CLI 到底适不适用于容器？

深度检测的结论是 **适用**：

| 判断维度 | 结论 |
| :--- | :--- |
| 官方定位 | CodeBuddy CLI 是**无头环境**工具，「不依赖图形界面，可在远程服务器、**Docker 容器**和 CI/CD runner 等无头环境中正常运行」 |
| 平台支持 | Linux x86_64 / arm64 官方支持；npm 包 `@tencent-ai/codebuddy-code`，要求 Node 18.20+（本镜像 Node 20 ✅） |
| 无头模式 | `codebuddy -p`（非交互）、`-y`、`--output-format json`、`CODEBUDDY_IS_SANDBOX=1` |
| 认证机制 | `apiKeyHelper` 是**官方 settings 配置项**，脚本在 `/bin/sh` 执行，输出作为 `X-Api-Key` 与 `Authorization: Bearer` |
| 上游设计 | wb-switch 二进制同时含 macOS / Windows / **Linux（`/usr/bin/codebuddy`）** 三套路径与 `helper.{sh,cmd,cjs}` 三平台 helper |

所以它**不是**「仅适用于 macOS 等桌面系统」，不能按这个理由移除。

## 2. 那之前的问题在哪？

配置早就写好了，但**缺了执行者**：

| 缺口 | 后果 |
| :--- | :--- |
| 容器里没有 CLI 二进制 | 没有任何进程会读 `settings.json` 或调用 `helper.cjs` → **空转** |
| `state.json` 不存在 | helper fallback 到 `accounts[0]`，绑定账号不确定，且与面板显示不一致 |
| 官方「已接入」判据只看配置文件 | 容器里没 CLI 也显示「已接入」→ **假象** |

## 3. 本版做了什么

### 镜像内置 CLI
- `npm i -g @tencent-ai/codebuddy-code` + `DISABLE_AUTOUPDATER=1`
- 补 `git`（CLI 的版本控制能力依赖）与 `/workspace` 工作目录
- 代价：镜像约 +175 MB

### 修复绑定账号不确定
- 新增 `gateway/cli_bootstrap.py`：启动时若 `state.json` 缺失，调用官方 `POST /api/codebuddy-cli/switch` 绑定一个账号（优先国际版 `variant=ai`，与默认端点 `codebuddy.ai` 匹配），由 wb-switch 自己写出格式正确的 `state.json`
- **不覆盖已存在的 `state.json`**，不影响手动选择与后续的自动轮换

### 消除「假接入」
- 新增 `GET /api/cli-info`：**真实探测** `codebuddy` 是否可执行并返回版本（缓存 60 秒）
- 设置页说明条重写，并实时显示探测结果（绿色 = 可用，橙色 = 未检测到）

---

## 快速开始

```bash
# 交互式
docker exec -it WorkBuddy-Switch codebuddy

# 无头 / 脚本化
docker exec WorkBuddy-Switch codebuddy -p "分析 /workspace 下的代码并总结" -y
```

认证链路（启动时自动完成，无需登录）：

```
~/.codebuddy/settings.json
  └─ apiKeyHelper → ~/.codebuddy-rotate/helper.cjs
       └─ 读 ~/.codebuddy-rotate/state.json 的 activeAccountId
            └─ 输出 "Bearer <该账号 token>" → CLI 作为认证头
```

## 升级注意

- 镜像体积增加约 175 MB（CLI 本体）。
- 已有 `/data` 数据卷无需迁移；`state.json` 会在首次启动时自动补齐。
- CLI 绑定账号与网关账号池**相互独立**：CLI 拿 token 后直接访问官方端点，不经过本网关。

## 接口变更

| 接口 | 变更 |
| :--- | :--- |
| `GET /api/cli-info` | **新增**：真实探测容器内 CLI 可用性（`installed` / `version` / `path`） |
