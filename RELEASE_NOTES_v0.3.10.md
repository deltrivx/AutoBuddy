# v0.3.10 - 容器内 CLI 运行条件补全

> v0.3.9 让 CodeBuddy CLI 在容器里跑起来了；本版把「跑起来」补成「跑得顺」。

## 改了什么

### 1. git 身份默认值

**问题**：容器内没有 git 身份配置（`git config --global --list` 为空）。CodeBuddy CLI 的版本控制能力（查看 diff、提交变更、创建分支）依赖 `user.name` / `user.email`，缺失时 commit 会直接报错。

**修复**：镜像内置 **system 级**默认身份：

```
user.name  = CodeBuddy CLI (container)
user.email = codebuddy@container.local
```

用 system 级（`/etc/gitconfig`）而非 global，是为了让你在容器里一条命令就能覆盖：

```bash
docker exec WorkBuddy-Switch git config --global user.name "你的名字"
docker exec WorkBuddy-Switch git config --global user.email "you@example.com"
```

### 2. `/workspace` 挂载位

Unraid 模板新增可选映射（高级选项，非必填）：

```
/workspace  ←  /mnt/user/appdata/workbuddy-switch/workspace
```

容器内 `/workspace` 目录在镜像构建时已预建。不挂载也能用 CLI，只是没有可操作的项目文件。

### 3. 自检确认无需改动

| 项 | 结果 |
| :--- | :--- |
| ripgrep | CLI **自带**（`vendor/ripgrep/arm64-linux/rg` 等多平台），无需另装 |
| DISABLE_AUTOUPDATER | `=1`，已生效 |
| Node / npm / git | v20.20.2 / 10.8.2 / 2.43.0 |
| 体积 | CLI 174M + workbuddy-switch 25M |

## 升级注意

- 本次仅镜像内新增 git 默认身份；已有 `/data` 数据卷无需迁移。
- 若你不希望使用默认身份，用 `git config --global` 覆盖即可（优先级高于 system）。
- Unraid 用户：模板已更新（原模板备份为 `.bak-v039`），重新应用模板后 `/workspace` 映射才会生效。
