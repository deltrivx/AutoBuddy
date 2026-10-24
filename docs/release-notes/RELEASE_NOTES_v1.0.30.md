镜像构建改为**只在打 tag 时执行一次**，`latest` 与版本号同批产出、指向同一个 digest。

## 一、问题：同一个 commit 被构建两遍

旧架构的 workflow 同时监听两种触发：

```yaml
on:
  push:
    branches: [main]
    tags: [v*]
```

于是发一次版会跑两遍构建：

| 触发 | 产出 tag | 结果 |
| :--- | :--- | :--- |
| `push main` | `latest` | digest A |
| `push tag v1.0.29` | `1.0.29` | digest B |

两次构建的 **11 个 layer 的 digest 逐字节相同**，`config.created` 精确到纳秒也相同，
但 OCI index 这一层的哈希不同（因为 index 里带的 tag 元数据不同）。

**这带来三个实际麻烦**：

1. 每次发版都要人工核对「A 和 B 是不是同一个镜像」——这本身就是设计缺陷。
2. `latest` 与版本号是两个不同的 digest，「最新版」没有唯一确定指向。
3. main 上任何提交都会重推 `latest`，让它指向一个**尚未发版**的中间状态。

## 二、修法：一次构建，一个 digest，多个 tag

```yaml
on:
  push:
    tags:
      - 'v*'
  workflow_dispatch:
```

`tags:` 规则改为三条，其中 `latest` 的启用条件从「main 或 release」改成「v* tag」：

```yaml
tags: |
  type=match,pattern=v(.*),group=1,enable=${{ startsWith(github.ref, 'refs/tags/v') }}
  type=semver,pattern={{version}}
  type=raw,value=latest,enable=${{ startsWith(github.ref, 'refs/tags/v') }}
```

于是打一次 tag：

```
push tag v1.0.30 → 一次 build → 打上 1.0.30 + latest → 同一个 digest
```

## 三、`flavor: latest=false` 为什么保留

查了 metadata-action v6 的源码（`src/meta.ts`），两条通道是分开的：

```typescript
// procRaw：raw 规则产出 "latest"，走的是 version.main
return Meta.setVersion(version, vraw, this.flavor.latest == 'auto' ? false : this.flavor.latest == 'true');

// setVersion 里只有第一次设置时才记录 latest 布尔
if (version.latest == undefined) { version.latest = latest; }

// generateTags 最后
if (this.version.latest) { tags.push('latest'); }
```

关键点：**`type=raw,value=latest` 的 `latest` 是通过 `version.main` 通道输出的**，
`flavor.latest` 管的是末尾那个「附加 latest」布尔，两者不冲突。

`flavor: latest=false` 保留的作用：抑制 `type=semver` 规则在非预发布版本上
自动附加的那条 `latest`（源码里 `latest = true`）。虽然 buildx 会去重，但显式关掉
语义更清楚，也不会因为 metadata-action 版本升级改变默认行为而出意外。

## 四、附带变化

| 项 | 变化 |
| :--- | :--- |
| `on.push.branches` | **移除**（不再监听 main 推送） |
| `on.push.paths-ignore` | **移除**（既然不监听 main，文档改动过滤已无意义） |
| `on.release` | **移除**（tag push 已经覆盖发版场景，避免重复构建） |
| `test` job | **保留**，每次 tag 构建照常跑全量测试 |
| 镜像 OCI label | **保留**此前修好的显式 `labels:`（version 取 `VERSION_DEFAULT`） |

## 五、影响

- **发版流程**：`git push origin v1.0.30` 一条命令即完成「构建镜像 + 打 latest + 建 Release」。
- **main 提交**：不再触发镜像构建，所以不会出现「latest 指向未发版代码」。
- **代价**：main 上的代码在没有打 tag 之前不会有镜像。这是有意的 —— 镜像只对应正式版本。

## 六、验证

新版本 v1.0.30 构建后核对：

| 检查项 | 期望 |
| :--- | :--- |
| build run 数量 | **1 个**（不是 2 个） |
| `1.0.30` 与 `latest` 的 index digest | **完全相同**（一字不差） |
| 两者 revision | 相同 |
| 两者 version label | 都是 `1.0.30` |
| Release | 自动创建，tag_name = `v1.0.30` |
