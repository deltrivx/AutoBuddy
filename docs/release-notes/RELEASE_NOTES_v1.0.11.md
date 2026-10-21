本版做两件事：**撤销四段日期制、回归语义化三位制**，以及**修掉两个 GHCR 标签缺陷**。

## 版本号回归语义化

版本号回到 `vX.Y.Z` 三位制：feature 进 minor，bugfix 进 patch，breaking 进 major。本版是 `v1.0.11`。

短暂试过的四段日期制（`v2026.10.21.03`）已废弃。原因很直接：**它不是标准 semver**，`docker/metadata-action` 的 `type=semver` 对它一条标签都产不出（源码里 `semver.valid()` 返回 null 后该规则静默跳过、不报错），tag 推送后 GHCR 会只剩 `latest`，容器拉版本标签直接落空。这与本仓既有的 `x.y.z` / `x.y` 标签体系合不来。

存量 `v1.0.10` 及更早全部沿用三位制，版本号形态自此统一。

## 修掉的第一个坑：镜像标签带 v 前缀

`type=ref,event=tag` 并不像文档注释里写的那样「自动剥掉 v 前缀」。源码 `procRefTag()` 只做 `replace(/^refs\/tags\//)`，官方文档表格也是 `refs/tags/v1.2.3` → `v1.2.3`。

实测后果：`v2026.10.21.03` 被推成了 `v2026.10.21.03`，与存量三段式 `1.0.10` / `1.0` 的形态不一致，也和 `RELEASES.md` 里 `gen_releases.py` 生成的 `lstrip('v')` 拉取命令不符 —— 文档让拉 `2026.10.21.03`，仓库里实际只有 `v2026.10.21.03`。

**修法**：改用 `type=match,pattern=v(.*),group=1`，靠捕获组剥 v。`v1.0.11` → `1.0.11`，再由 semver 规则补 `1.0`。

## 修掉的第二个坑：latest 标签重复产出

CI 日志里 `Docker tags` 出现了两条 `latest` —— `type=raw,value=latest` 产一条，`type=match` / `type=semver` 在 `latest=auto` 下又各产一条。buildx 会自动去重，无实际影响，但已在 workflow 注释里记明，免得下次再当新问题查。

## 保留了的东西

`scripts/gen_releases.py` 里 `version_key()` 补零对齐段长的逻辑保留。这是通用稳健性 —— 原来的实现直接按段数比，`[0,4,5]` 和 `[2026,10,21,3]` 这类不等长列表直接比较会出错乱。对三位制同样有益。

`_test_version_sync.py` 的正则仍兼容四段形态，避免历史版本段被误判；但断言已回归三位制。

## 测试与验证

- `_test_version_sync.py` 全绿，含「CHANGELOG 最新版本与 `main.py` 常量一致」这条老护栏。
- 全量测试文件回归全绿。
- CI、tag、Release、部署均实测通过，`/health` 报 `1.0.11`。
