## 🌟 改进亮点

### 1. 账号卡片模型清单改为动态可用清单
- **口径统一**：账号卡片下方展示的模型，数据源从「官方 usage 里调用过的模型」改为**网关 `/v1/models` 的完整可路由清单**。此前全局网关显示 32 款、卡片只显示 23 款（仅统计已调用模型），现在两者完全一致。
- **已调用高亮**：该账号真实调用过的模型单独着色，鼠标悬停显示「调用次数 · 消耗积分」，一眼区分「可用」与「已用」。
- **新账号不再留空**：刚添加、还没有任何调用记录的账号同样展示完整清单，标题标注「暂无调用记录」，不会再出现整块空白。
- 完全不硬编码：网关侧清单来自内置基础清单 + 官方调用流水自动发现，上游新增模型会自动出现。

### 2. 移除容器内无效的桌面程序状态图标
- 账号管理页右上角原本有 3 个状态图标（WorkBuddy / CodeBuddy IDE / CodeBuddy CLI），检测对象是**宿主机上的桌面客户端**。容器里根本不存在这些程序，状态恒为「未运行 / 未安装」，悬停提示（如「workbuddy 未运行」）只会误导，本版本整组移除。
- 同时继续隐藏「设为 CodeBuddy IDE / CLI 当前账号」等点击必然失败的按钮，以及「无 Buddy」等无参考价值的状态标签。

### 3. 模型获取链路与仓库清理
- 修正 GitHub Actions 中残留的 `type=raw,value=v0.1.0` 标签规则——此前每次构建都会把 `v0.1.0` 标签覆盖到最新镜像。
- 删除仓库根目录下未被镜像使用的重复 `main.py` / `web_proxy.py` / `token_tracker.py`（Dockerfile 实际只打包 `gateway/`），消除「改了不生效」的陷阱。

### 4. 文档与日志
- 补全 CHANGELOG 中缺失的 v0.3.1 条目，与既有格式对齐。
- README 补齐自动发现的 7 款模型（`deepseek-v4.1-flash`、`hy4-preview-f`、`claude-opus-4.6`、`claude-sonnet-4.6`、`kimi-k2.7`、`glm-5.1`、`codewise-model-a9`）。
- 新增「网关内置接口」说明（`/v1/models`、`/health`、`/rotate/status`、`/rotate/run`、`/api/account-models`）。

---

## 📦 升级方式

Unraid 用户：拉取 `ghcr.io/deltrivx/workbuddy-switch:latest` 后用容器模板重建即可，配置文件与账号数据均在 `/data` 持久化，无需重新添加账号。

```bash
docker pull ghcr.io/deltrivx/workbuddy-switch:latest
```

> 注意：请始终通过容器模板（Unraid Template）重建容器，不要手工拼接 `docker run`，以免遗漏挂载与环境变量。
