"""版本号一致性：网关报出来的版本必须与 CHANGELOG 里最新发布的版本相同。

为什么需要这个测试：面板上显示的版本来自 `gateway/main.py` 的常量，而用户判断
「我跑的是不是最新」看的是 GitHub Release 标签。两者一旦不同步，用户就只能猜。
历史上它们**本来就是两套号**（网关 1.8.0 / 发布 0.4.4），这个测试就是那次事故的
护栏：常量与 CHANGELOG 对不上，CI 直接失败，镜像根本推不出去。

同时校验发布说明文件存在 —— Release body 必须与 `docs/release-notes/*.md` 逐字一致，
漏写 notes 文件会让两者无源可对。

只依赖标准库，与其它 `_test_*.py` 一样可以直接跑。
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent

# 匹配「像版本号」的串。两侧都排除数字和点，否则 `127.0.0.1` 这种地址
# 会被切成 `127.0.0` 误报。
VERSION_RX = re.compile(r"(?<![\d.])v?\d+\.\d+\.\d+(?![\d.])")

_ok = 0
_fail = []


def check(label, condition, detail=""):
    global _ok
    if condition:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _fail.append(label)
        print(f"  FAIL {label} {detail}")


print("[1] 网关版本常量")
src = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")

matched = re.search(r'^VERSION_DEFAULT\s*=\s*"([^"]+)"\s*$', src, re.M)
check("main.py 里有唯一的版本常量 VERSION_DEFAULT", matched is not None)
version = matched.group(1) if matched else ""
check("版本号形如 x.y.z", bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), f"got {version!r}")

# 常量必须被真正用起来：FastAPI 元信息与 /health、/gateway/info 都读它。
# 只要有一处还写着字面量，界面上就会出现第二个版本号。
check("FastAPI 元信息用的是常量而不是字面量",
      "version=GATEWAY_VERSION" in src)
check("没有残留的硬编码版本号字面量",
      not re.search(r'version="\d+\.\d+\.\d+"', src), "发现 version=\"x.y.z\" 字面量")
check("版本只有一处定义（VERSION_DEFAULT）",
      len(re.findall(r'^VERSION_DEFAULT\s*=', src, re.M)) == 1)

print("\n[2] 与 CHANGELOG 对齐")
changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
# 版本段标题形如 `## [v0.4.5] - 2026-09-19`（方括号里允许带 v 前缀）
released = re.findall(r"^##\s*\[v?(\d+\.\d+\.\d+)\]", changelog, re.M)
check("CHANGELOG 里有已发布的版本段", bool(released), f"got {released[:5]}")
if released:
    check(f"最新发布版本就是 {version}", released[0] == version,
          f"CHANGELOG 最新是 {released[0]}，main.py 是 {version}")
check(f"CHANGELOG 里存在 [{version}] 段", version in released, f"got {released[:5]}")
check("CHANGELOG 保留 [未发布] 段", "## [未发布]" in changelog)

# 文末链接区由人维护，漏一行不会有任何报错 —— 只是 GitHub 上「对比上一版」的链接失效。
# v0.4.6 就漏过：链接区从 v0.4.5 直接跳到 HEAD，中间那一版无法对比。
links = set(re.findall(r"^\[v(\d+\.\d+\.\d+)\]:", changelog, re.M))
missing_links = [v for v in released if v not in links]
check("每个已发布版本都有 compare 链接", not missing_links, f"缺少 {missing_links}")
check("[未发布] 段的对比基准是最新发布版本",
      bool(re.search(rf"^\[未发布\]:.*compare/v{re.escape(version)}\.\.\.HEAD", changelog, re.M)),
      f"基准应指向 v{version}")

print("\n[3] 与发布说明文件对齐")
notes = ROOT / "docs" / "release-notes" / f"RELEASE_NOTES_v{version}.md"
check(f"docs/release-notes/RELEASE_NOTES_v{version}.md 存在", notes.exists())
if notes.exists():
    body = notes.read_text(encoding="utf-8").lstrip("\ufeff")
    first = next((ln for ln in body.split("\n") if ln.strip()), "")
    # 正文第一行不能是 H1 版本标题：GitHub 渲染时标题已经单独显示了，
    # 正文再写一遍就是重复（这条规矩踩过不止一次）。
    check("发布说明首行不是重复的版本标题",
          not re.fullmatch(r"#\s*v?\d+\.\d+\.\d+.*", first.strip()),
          f"first line = {first.strip()!r}")

print("\n[4] README 不内嵌版本号")
readme_versions = VERSION_RX.findall((ROOT / "README.md").read_text(encoding="utf-8"))
check("README 不出现版本号（版本只在 CHANGELOG / Release 里）",
      not readme_versions, f"got {readme_versions[:5]}")

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
