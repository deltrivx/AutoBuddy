镜像 OCI `version` label 与 `RELEASES.md` 详细说明链接修正后的首个正式版本。

## 一、镜像 `org.opencontainers.image.version` label

### 问题

`latest` 镜像的这个 label 是字面量 `latest`，不是真实版本号。`docker inspect` 读出来
分不清镜像究竟是哪个版本。

### 根因

`.github/workflows/docker-build-ghcr.yml` 里 `docker/metadata-action` 没有显式
`labels:` 输入。它按默认行为把 `org.opencontainers.image.version` 设成「本次产出的
**第一个** tag 名」：

| 构建场景 | 产出的 tag | version label |
| :--- | :--- | :--- |
| push tag `v1.0.28` | `1.0.28` | `1.0.28` ✅ |
| push main / release | `latest` | **`latest`** ❌ |

### 修法

新增 `Resolve version` 步骤，从**单一版本源** `gateway/main.py` 的 `VERSION_DEFAULT`
解析真实版本号，再用 `labels:` 显式覆盖：

```yaml
- name: Resolve version
  id: ver
  run: |
    V="$(grep -oP 'VERSION_DEFAULT\s*=\s*"\K[^"]+' gateway/main.py)"
    echo "version=$V" >> "$GITHUB_OUTPUT"

- name: Extract Docker metadata
  with:
    labels: |
      org.opencontainers.image.version=${{ steps.ver.outputs.version }}
      org.opencontainers.image.revision=${{ github.sha }}
      org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}
      org.opencontainers.image.title=AutoBuddy
```

**为什么不用 `github.ref_name`**：tag 场景是 `v1.0.28`（带 `v`，与 docker tag 的
`1.0.28` 不一致）；push main 场景是 `main`（根本不是版本号）。两种都不对。

**为什么不用 `steps.meta.outputs.version`**：同理会随 tag 形态漂移。

`VERSION_DEFAULT` 是唯一源 —— `tests/test_version_sync.py` 强制它与 CHANGELOG
最新版本一致，任何触发场景读它都得到正确版本号。

## 二、`RELEASES.md` 的「详细说明」列

### 问题

`v1.0.27`、`v1.0.28` 两行的「发布说明」列显示 `—` 而不是链接。

### 根因

`scripts/gen_releases.py` 用 `note.exists()` 判断 RELEASE_NOTES 文件是否存在于
**本地磁盘**。本地跑脚本时新版本 NOTES 往往还没写完/还没提交，于是降级成 `—`。
更糟的是判断随机器环境而变，同一份 CHANGELOG 在不同环境产出不同的 RELEASES.md，不幂等。

### 修法

改为只看 CHANGELOG 是否有该版本段：

```python
link = f"[详细说明](./docs/release-notes/{note.name})"
```

依据是仓库铁律：版本出现在 CHANGELOG 里就一定有对应的发布说明，这一条由
`tests/test_version_sync.py` 强制校验（NOTES 文件必须存在，且 Release body
与它逐字一致）。真缺文件时由那个测试拦住，而不是在这里静默降级成 `—`。

**为什么不选「CI 里跑脚本并自动提交」**：会形成「提交触发 CI → CI 再提交」的
循环风险，且需要给 workflow 写权限，收益不值当。

## 三、影响

| 项 | 说明 |
| :--- | :--- |
| 运行时业务代码 | **零改动** |
| 镜像内容 | 仅 OCI label 变化 |
| 用户可见 | `docker inspect` 的 `version` 现在始终是真实版本号；RELEASES 索引表链接完整 |

## 四、验证

- CI run #698：`Resolve version` 输出 `镜像版本号: 1.0.28`，`latest` 镜像的
  `org.opencontainers.image.version` 实测为 `1.0.28`（不再是 `latest`）。
- `RELEASES.md` 重新生成后 `v1.0.27` / `v1.0.28` 均有「详细说明」链接，全表
  仅剩 1 行 `—`（极早期无 NOTES 的版本）。
