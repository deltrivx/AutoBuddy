"""对官方 wb-switch 二进制内的前端 JS 做等长字节补丁。

容器适配原则：依赖宿主桌面客户端的功能直接从 JSX 物理移除，不依赖 CSS/MutationObserver
遮掩。所有替换严格等长，避免破坏二进制中的偏移表。
"""

import sys


def _scan_expr(data: bytes, start: int) -> int:
    """扫描一个完整 JS 调用/对象表达式，返回结束位置（不含）。"""
    depth = 0
    i = start
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if c in (b'"', b"'", b"`"):
            quote = c
            i += 1
            while i < n:
                ch = data[i:i + 1]
                if ch == b"\\":
                    i += 2
                    continue
                if quote == b"`" and data[i:i + 2] == b"${":
                    inner_end = _scan_expr(data, i + 1)
                    if inner_end < 0:
                        return -1
                    i = inner_end
                    continue
                if ch == quote:
                    i += 1
                    break
                i += 1
            continue
        if c in (b"(", b"[", b"{"):
            depth += 1
        elif c in (b")", b"]", b"}"):
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _nullify_expr(data: bytes, anchor: bytes, label: str) -> bytes:
    """从 anchor 开始移除一个完整 JS 表达式，保留等长空白。"""
    idx = data.find(anchor)
    if idx < 0:
        print(f"- skip {label}: anchor not found")
        return data
    end = _scan_expr(data, idx)
    if end < 0:
        print(f"! skip {label}: expression not balanced")
        return data
    old = data[idx:end]
    new = b"null" + b" " * (len(old) - 4)
    assert len(old) == len(new)
    print(f"✓ remove {label}: {len(old)} bytes")
    return data[:idx] + new + data[end:]


def _nullify_function_return(data: bytes, anchor: bytes, label: str) -> bytes:
    idx = data.find(anchor)
    if idx < 0:
        print(f"- skip {label}: function not found")
        return data
    ret = data.find(b"return ", idx)
    if ret < 0:
        print(f"- skip {label}: return not found")
        return data
    start = ret + len(b"return ")
    end = _scan_expr(data, start)
    if end < 0:
        print(f"! skip {label}: return expression not balanced")
        return data
    old = data[start:end]
    new = b"null" + b" " * (len(old) - 4)
    assert len(old) == len(new)
    print(f"✓ remove {label}: {len(old)} bytes")
    return data[:start] + new + data[end:]


def _replace_equal(data: bytes, old: bytes, new: bytes, label: str) -> bytes:
    if old not in data:
        print(f"- skip {label}: text not found")
        return data
    assert len(old) == len(new), f"{label}: unequal byte length"
    print(f"✓ {label}")
    return data.replace(old, new)


def patch(bin_path: str) -> None:
    with open(bin_path, "rb") as f:
        data = f.read()
    print(f"patch target: {bin_path} ({len(data)} bytes)")

    data = _replace_equal(
        data,
        b'const Db="http://127.0.0.1:57890";',
        b'const Db=window.location.origin;  ',
        "backend endpoint -> window.location.origin",
    )

    # 账号卡片头部（账号名右侧）的图标版产品槽位。
    # 与底部 footer 是同一组功能：WorkBuddy / CodeBuddy IDE / CodeBuddy CLI，
    # 无论显示成「当前账号」徽章还是「设为当前账号 / 切换到…」按钮，
    # 都会去调用宿主桌面程序，容器里必然失败。整行移除。
    data = _nullify_expr(
        data,
        b'm.jsxs("div",{className:"ml-auto flex shrink-0 items-center gap-1"',
        "account-card header product icon row",
    )

    # 账号卡片底部三个产品槽位：
    # WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 都只会调用宿主桌面程序。
    # 物理移除整个 footer，避免按钮和 Kb 当前账号徽章再次出现。
    data = _nullify_expr(
        data,
        b'm.jsxs("footer",{className:"flex flex-wrap items-center gap-2.5 border-t px-5 py-2.5"',
        "account-card WorkBuddy/IDE/CLI footer",
    )

    # 其他页面复用的 Kb 徽章也不保留。
    data = _nullify_function_return(
        data,
        b"function Kb({product:",
        "Kb current-account badge component",
    )

    # 账号管理页右上角三个桌面程序运行状态图标。
    data = _nullify_expr(
        data,
        b'm.jsx("div",{className:"flex shrink-0 items-center gap-4 pt-1"',
        "desktop runtime status icon group",
    )

    # 导入本机账号：依赖宿主机客户端文件。
    data = _nullify_expr(
        data,
        b'm.jsx(Dt,{children:m.jsxs(Oe,{className:"h-10 px-4",onClick:pl',
        "import-local-account button",
    )

    # 设置页权限检测：检测 macOS 完全磁盘访问权限，容器不存在该能力。
    data = _nullify_expr(data, b"m.jsx(_ye,{})", "desktop permission-check module")

    # Token 统计页桌面端来源分组 Tab。
    data = _nullify_expr(
        data,
        b'm.jsx(YO,{className:"mb-8 min-w-0 gap-0"',
        "desktop token-source tabs",
    )

    data = _nullify_expr(
        data,
        b'm.jsx(Vd,{id:"token-overview-title"',
        "redundant token-overview title",
    )

    data = _nullify_expr(
        data,
        b'n==="codebuddy-cli"&&m.jsxs(Oe,{className:"shrink-0"',
        "redundant request-detail button",
    )

    data = _replace_equal(
        data,
        "自动签到、权限检测与自动更新配置。".encode("utf-8"),
        "自动签到、账号保活与接口轮换配置。".encode("utf-8"),
        "settings subtitle",
    )

    with open(bin_path, "wb") as f:
        f.write(data)
    print("patch complete")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_binary.py <wb-switch-binary>")
    patch(sys.argv[1])
