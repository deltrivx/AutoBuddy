#!/usr/bin/env python3
"""从 CHANGELOG.md 生成 RELEASES.md 版本索引。

版本索引必须和 CHANGELOG 保持一致，手工维护早晚会漏。改完 CHANGELOG 后跑一次：

    python3 scripts/gen_releases.py

规则：
- 版本号与日期取自 CHANGELOG 的 `## [vX.Y.Z] - YYYY-MM-DD` 标题行；
- 摘要取自该版本下的第一个 `###` 小标题（去掉 emoji 前缀）；
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
    entries = []
    current = None
    for line in text.split("\n"):
        header = re.match(r"^## \[(v[^\]]+)\] - (\S+)", line)
        if header:
            current = {"version": header.group(1), "date": header.group(2), "title": ""}
            entries.append(current)
            continue
        if current and not current["title"]:
            title = re.match(r"^###\s+(.*)", line)
            if title:
                # 去掉开头的 emoji，保留可读标题
                current["title"] = re.sub(r"^[^\w\u4e00-\u9fff]+\s*", "", title.group(1).strip())
    return entries


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

    OUTPUT.write_text(body, encoding="utf-8")
    print(f"已写入 {OUTPUT.relative_to(REPO)}：共 {len(entries)} 个版本，当前稳定版 {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
