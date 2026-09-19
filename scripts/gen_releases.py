#!/usr/bin/env python3
"""从 CHANGELOG.md 生成 RELEASES.md 版本索引。

版本索引必须和 CHANGELOG 保持一致，手工维护早晚会漏。改完 CHANGELOG 后跑一次：

    python3 scripts/gen_releases.py

规则：
- 版本号与日期取自 CHANGELOG 的 `## [vX.Y.Z] - YYYY-MM-DD` 标题行；
- 摘要取自该版本标题行下方的**首段正文**（Keep a Changelog 里这一段就是本版一句话概述）；
  若首段为空或缺失，则回退到该版本第一个 `###` 分类小标题；
- 若 docs/release-notes/RELEASE_NOTES_vX.Y.Z.md 存在，则附上「详细说明」链接；
- 列表按语义化版本号倒序（GitHub Releases 侧栏按发布时间排序，本页按版本号排序，
  避免后补的旧版本让顺序看起来错乱）。
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHANGELOG = REPO / "CHANGELOG.md"
OUTPUT = REPO / "RELEASES.md"
NOTES_DIR = REPO / "docs" / "release-notes"

REPO_SLUG = "deltrivx/workbuddy-switch"


def parse_versions(text: str):
    """解析 CHANGELOG，抽出 (版本号, 日期, 一句话摘要)。"""
    # 版本标题 -> 该版本自己的正文块（到下一个版本标题为止）
    blocks = re.split(r"^## \[(v[^\]]+)\] - (\S+)", text, flags=re.MULTILINE)
    entries = []
    # blocks: [前言, ver1, date1, body1, ver2, date2, body2, ...]
    for i in range(1, len(blocks) - 1, 3):
        version, date, body = blocks[i], blocks[i + 1], blocks[i + 2]
        entries.append({"version": version, "date": date, "title": _summary(body)})
    return entries


def _summary(body: str) -> str:
    """取一段适合放进索引表的摘要。

    摘要以 `<!-- summary: … -->` 注释的形式写在 CHANGELOG 里，由人写死 ——
    从正文猜出来的只会是半句话（正文首段常是背景铺垫），放进索引表毫无意义。
    没有该注释时回退到第一个 `###` 分类小标题（去掉 emoji 前缀）。
    """
    m = re.search(r"<!--\s*summary:\s*(.+?)\s*-->", body)
    if m:
        return m.group(1).strip()

    for line in body.split("\n"):
        if line.strip().startswith("###"):
            return re.sub(r"^[^\w\u4e00-\u9fff]+\s*", "", line.strip().lstrip("#").strip())
    return "—"


def version_key(entry):
    try:
        return [int(x) for x in entry["version"].lstrip("v").split(".")]
    except ValueError:
        return [0]


def main() -> int:
    if not CHANGELOG.exists():
        print(f"找不到 {CHANGELOG}", file=sys.stderr)
        return 1

    entries = parse_versions(CHANGELOG.read_text(encoding="utf-8"))
    if not entries:
        print("CHANGELOG 里没有解析到任何版本，检查标题格式是否为 `## [vX.Y.Z] - YYYY-MM-DD`", file=sys.stderr)
        return 1
    entries.sort(key=version_key, reverse=True)

    latest = entries[0]["version"]
    rows = []
    for entry in entries:
        note = NOTES_DIR / f"RELEASE_NOTES_{entry['version']}.md"
        link = f"[详细说明](./docs/release-notes/{note.name})" if note.exists() else "—"
        rows.append(
            f"| [{entry['version']}](https://github.com/{REPO_SLUG}/releases/tag/{entry['version']}) "
            f"| {entry['date']} | {entry['title']} | {link} |"
        )

    body = f"""# WorkBuddy Switch Releases

[项目说明](README.md) | [更新日志](CHANGELOG.md)

本页是 **WorkBuddy Switch 的版本索引**。GitHub Releases 侧栏按发布时间排序，本页按语义化版本号排序，
避免后补的旧版本让版本顺序看起来错乱。

**当前稳定版：** [{latest}](https://github.com/{REPO_SLUG}/releases/tag/{latest})

> 本文件由 `scripts/gen_releases.py` 从 `CHANGELOG.md` 生成，修改请改 CHANGELOG 后重新生成。

## 版本索引

| Version | 日期 | 更新摘要 | 发布说明 |
|---|---|---|---|
{chr(10).join(rows)}

## 镜像标签

镜像由 GitHub Actions **云端构建**并推送至 GHCR（不在本地构建上传）：

```bash
docker pull ghcr.io/{REPO_SLUG}:latest   # 最新稳定版
docker pull ghcr.io/{REPO_SLUG}:{latest.lstrip('v')}    # 锁定版本
```

## 部署产物

每个 Release 均附带以下部署产物：

| 产物 | 说明 |
|---|---|
| `WorkBuddy-Switch.xml` | Unraid 容器模板（**请以模板方式创建容器，勿手工拼接 `docker run`**） |
| `docker-compose.yml` | Docker Compose 部署文件 |
| `icon.png` | 512×512 容器图标（Unraid 模板使用） |
| `SHA256SUMS` | 上述产物的校验值 |
"""

    # 仓库约定 LF（见 .gitattributes）。Windows 上 write_text 默认会写成 CRLF，
    # 所以这里显式走 bytes 并断言，避免产物行尾与仓库不一致。
    data = body.replace("\r\n", "\n").encode("utf-8")
    assert b"\r\n" not in data, "生成结果含 CRLF"
    OUTPUT.write_bytes(data)
    print(f"已写入 {OUTPUT.relative_to(REPO)}：共 {len(entries)} 个版本，当前稳定版 {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
