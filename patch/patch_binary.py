"""对官方 wb-switch 二进制内的前端 JS 做等长字节补丁。

容器适配原则
------------
依赖宿主桌面客户端的功能直接从 JSX 物理移除（整块替换为等长空白），
不依赖 CSS 遮掩，也不依赖 MutationObserver。所有替换严格等长，否则会
破坏二进制内的偏移表。

两条硬约束（v0.3.11 起）
-----------------------
1. 锚点不得依赖压缩变量名
   官方每次发版都会重排 minify 变量名：0.1.36 是 ``m.jsx`` / ``Db``，
   0.1.40 变成 ``p.jsx`` / ``zb``。把 ``m.jsx`` 写死在锚点里，一发版
   全部失配。因此本脚本一律用「稳定的字符串字面量 / className / id」
   定位，再回溯到 JSX 调用起点；变量名从匹配结果里现取，不写死。

2. 锚点失配必须让构建失败
   历史上 Dockerfile 用 ``... 2>/dev/null || true`` 吞掉了失败，导致补丁
   全部失效但镜像构建 success，线上回归（三个桌面图标复活）无人察觉。
   本脚本收集所有失败项，末尾以非零码退出。

用法::

    python3 patch_binary.py <wb-switch-binary> [<binary> ...]
"""

import re
import sys

# ---------------------------------------------------------------------------
# 结果登记
# ---------------------------------------------------------------------------
_APPLIED = []
_FAILED = []

_JSX_CALL = re.compile(rb'([A-Za-z_$][A-Za-z0-9_$]*)\.jsxs?\(')
_FUNC_DEF = re.compile(rb'function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(')
_QUOTES = (b'"', b"'", b"`")


def _ok(label, detail=""):
    _APPLIED.append(label)
    print("  ok   %s%s" % (label, (" (%s)" % detail) if detail else ""))


def _fail(label, detail=""):
    _FAILED.append((label, detail))
    print("  FAIL %s%s" % (label, (" - %s" % detail) if detail else ""))


# ---------------------------------------------------------------------------
# 词法扫描（跳过字符串 / 模板串，括号配平）
# ---------------------------------------------------------------------------
def _skip_quoted(data, i):
    """data[i] 是引号，返回闭合引号之后的位置。"""
    quote = data[i:i + 1]
    i += 1
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if c == b"\\":
            i += 2
            continue
        if quote == b"`" and data[i:i + 2] == b"${":
            inner = _scan_expr(data, i + 1)
            if inner < 0:
                return n
            i = inner
            continue
        if c == quote:
            return i + 1
        i += 1
    return n


def _scan_expr(data, start):
    """扫描一个完整表达式（以 ``(`` / ``[`` / ``{`` 开头），返回结束位置。"""
    depth = 0
    i = start
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if c in _QUOTES:
            i = _skip_quoted(data, i)
            continue
        if c in (b"(", b"[", b"{"):
            depth += 1
        elif c in (b")", b"]", b"}"):
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _splice_null(data, start, end, label):
    """把 data[start:end] 换成 ``null`` + 等长空白。"""
    old = data[start:end]
    if len(old) < 4:
        _fail(label, "expression too short to nullify (%d bytes)" % len(old))
        return data
    new = b"null" + b" " * (len(old) - 4)
    assert len(old) == len(new)
    _ok(label, "removed %d bytes" % len(old))
    return data[:start] + new + data[end:]


# ---------------------------------------------------------------------------
# 定位工具
# ---------------------------------------------------------------------------
def _function_body(data, func_start):
    """给定 ``function NAME(`` 的起点，返回函数体 ``{...}`` 的区间。"""
    i = data.find(b"(", func_start)
    if i < 0:
        return None
    depth = 0
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body_start = data.find(b"{", i)
    if body_start < 0:
        return None
    body_end = _scan_expr(data, body_start)
    if body_end < 0:
        return None
    return (body_start, body_end)


def _call_start_before(data, pos, levels=1):
    """返回包住 pos 的第 ``levels`` 层 ``X.jsx(`` / ``X.jsxs(`` 调用起点。

    levels=1 是最内层（紧包住 marker 的那个调用），levels=2 是它的外层包裹。

    注意不能简单地取「marker 之前最近的 ``.jsx(`` 正则匹配」——marker 常常落在
    ``children:[a?X.jsx(..):Y.jsx(..), ...]`` 这种三元里，最近的匹配是**兄弟**
    调用而不是**包住**它的调用。必须真正做包含性判断。
    """
    p = pos
    start = -1
    for _ in range(levels):
        start = -1
        for m in reversed(list(_JSX_CALL.finditer(data, 0, p))):
            end = _scan_expr(data, m.start())
            if end > 0 and m.start() < p < end:
                start = m.start()
                break
        if start < 0:
            return -1
        p = start
    return start


def _enclosing_function(data, pos):
    """返回包住 pos 的最内层 ``function NAME(...){...}`` 的函数体区间。"""
    for m in reversed(list(_FUNC_DEF.finditer(data, 0, pos))):
        body = _function_body(data, m.start())
        if body and body[0] < pos < body[1]:
            return body
    return None


def _toplevel_return(data, body_start):
    """在函数体内找到**顶层** ``return `` 的位置，返回表达式起点。

    跳过嵌套函数 / 回调里的 return，避免改错地方。
    """
    depth = 0
    i = body_start
    n = len(data)
    found = -1
    while i < n:
        c = data[i:i + 1]
        if c in _QUOTES:
            i = _skip_quoted(data, i)
            continue
        if c == b"{":
            depth += 1
        elif c == b"}":
            depth -= 1
            if depth == 0:
                return found
        elif depth == 1 and data[i:i + 7] == b"return ":
            found = i + 7
            i += 7
            continue
        i += 1
    return found


def _unique_marker(data, marker, label):
    idx = data.find(marker)
    if idx < 0:
        _fail(label, "marker not found: %r" % marker[:60])
        return -1
    if data.find(marker, idx + 1) >= 0:
        _fail(label, "marker is not unique: %r" % marker[:60])
        return -1
    return idx


# ---------------------------------------------------------------------------
# 补丁动作
# ---------------------------------------------------------------------------
def _nullify_marker(data, marker, label, levels=1):
    """按稳定 marker 定位，回溯到 JSX 调用起点，整块抹成 null。"""
    idx = _unique_marker(data, marker, label)
    if idx < 0:
        return data
    start = _call_start_before(data, idx, levels)
    if start < 0:
        _fail(label, "no enclosing JSX call above marker")
        return data
    end = _scan_expr(data, start)
    if end < 0 or end <= idx:
        _fail(label, "unbalanced JSX call")
        return data
    return _splice_null(data, start, end, label)


def _nullify_after(data, marker, label):
    """marker 之后的表达式抹成 null（用于 ``cond&&<jsx>`` 这种短路写法）。"""
    idx = _unique_marker(data, marker, label)
    if idx < 0:
        return data
    start = idx + len(marker)
    end = _scan_expr(data, start)
    if end < 0:
        _fail(label, "unbalanced expression after marker")
        return data
    return _splice_null(data, start, end, label)


def _nullify_component_return(data, pattern, label):
    """按「组件定义特征」正则定位函数，把它的顶层 return 抹成 null。

    ``pattern`` 必须从 ``function`` 关键字开始匹配（例如
    ``function\\s+[A-Za-z_$][\\w$]*\\(\\{product:``），这样函数起点即匹配起点，
    从而**不依赖压缩后的组件名**。
    """
    hits = list(re.finditer(pattern, data))
    if len(hits) != 1:
        _fail(label, "expected exactly 1 component definition, got %d" % len(hits))
        return data
    body = _function_body(data, hits[0].start())
    if body is None:
        _fail(label, "function body not found")
        return data
    ret = _toplevel_return(data, body[0])
    if ret < 0:
        _fail(label, "top-level return not found")
        return data
    end = _scan_expr(data, ret)
    if end < 0:
        _fail(label, "unbalanced return expression")
        return data
    return _splice_null(data, ret, end, label)


def _nullify_section(data, section_id, label):
    """按 ``id:"settings-xxx"`` 定位设置页 section，把宿主组件整块停掉。

    只依赖 section id（稳定），不依赖压缩后的组件名（会漂移）。
    """
    idx = _unique_marker(data, ('id:"%s"' % section_id).encode(), label)
    if idx < 0:
        return data
    body = _enclosing_function(data, idx)
    if body is None:
        _fail(label, "no enclosing component for %s" % section_id)
        return data
    ret = _toplevel_return(data, body[0])
    if ret < 0:
        _fail(label, "no top-level return in %s" % section_id)
        return data
    end = _scan_expr(data, ret)
    if end < 0:
        _fail(label, "unbalanced return in %s" % section_id)
        return data
    return _splice_null(data, ret, end, label)


def _replace_padded(data, pattern, build, label):
    """正则整段替换，右侧补空格凑成等长。``build`` 返回新字节串或 None。"""
    hits = list(re.finditer(pattern, data))
    if len(hits) != 1:
        _fail(label, "expected exactly 1 match, got %d" % len(hits))
        return data
    m = hits[0]
    old = m.group(0)
    new = build(m)
    if new is None:
        _fail(label, "replacement text longer than original")
        return data
    if len(new) > len(old):
        _fail(label, "replacement longer than original (%d > %d)" % (len(new), len(old)))
        return data
    new = new + b" " * (len(old) - len(new))
    assert len(new) == len(old)
    _ok(label, "%d bytes" % len(old))
    return data[:m.start()] + new + data[m.end():]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
# 设置页总描述。原文案提到的「限额监听 / 权限检测 / 自动更新」都已被移除，
# 必须同步改写；长度不足 66 字节时由 _replace_padded 右侧补空格。
SETTINGS_SUBTITLE = "自动签到与账号保活，对外提供 OpenAI 兼容接口。"

# 账号卡片底部「导入本机…」按钮的 label，用于定位整个按钮（含 Tooltip 包裹）。
IMPORT_LOCAL_LABEL = "导入本机国际版账号\":\"导入本机账号"


def patch(bin_path):
    with open(bin_path, "rb") as f:
        data = f.read()
    size0 = len(data)
    print("patch target: %s (%d bytes)" % (bin_path, size0))

    # --- 1. 后端端点改成本页 origin，避免硬编码 127.0.0.1:57890 跨域 -------------
    # 变量名会漂移（Db -> zb），所以从匹配结果里取名字，只换值。
    data = _replace_padded(
        data,
        rb'const ([A-Za-z_$][A-Za-z0-9_$]*)="http://127\.0\.0\.1:57890";',
        lambda m: b"const " + m.group(1) + b"=window.location.origin;",
        "backend endpoint -> window.location.origin",
    )

    # --- 2. 账号卡片头部三个桌面图标 -------------------------------------------
    # WorkBuddy / CodeBuddy IDE / CodeBuddy CLI，无论显示成「当前账号」徽章还是
    # 「设为当前账号」按钮，点击都会去调用宿主桌面程序，容器里必然失败。
    data = _nullify_marker(
        data,
        b'className:"ml-auto flex shrink-0 items-center gap-1"',
        "account-card header product icon row",
    )

    # --- 3. 账号卡片底部三个产品槽位 -------------------------------------------
    data = _nullify_marker(
        data,
        b'className:"flex flex-wrap items-center gap-2.5 border-t px-5 py-2.5"',
        "account-card WorkBuddy/IDE/CLI footer",
    )

    # --- 4. 「当前账号」徽章组件（其他页面复用的那个） --------------------------
    # 用解构参数名 {product: 定位，不写死压缩后的组件名（0.1.36 是 Kb，0.1.40 是 Qb）。
    data = _nullify_component_return(
        data,
        rb'function\s+[A-Za-z_$][A-Za-z0-9_$]*\(\{product:',
        "current-account badge component",
    )

    # --- 5. 账号管理页右上角桌面程序运行状态图标 -------------------------------
    data = _nullify_marker(
        data,
        b'className:"flex shrink-0 items-center gap-4 pt-1"',
        "desktop runtime status icon group",
    )

    # --- 6. 导入本机账号按钮（依赖宿主机客户端文件） ---------------------------
    # marker 是按钮 label，需向上回溯两层：Se(按钮) -> Dt(Tooltip 包裹)。
    data = _nullify_marker(
        data,
        IMPORT_LOCAL_LABEL.encode("utf-8"),
        "import-local-account button",
        levels=2,
    )

    # --- 7. Token 统计页「数据来源」Tab（桌面端才有 IDE / CLI 来源） -----------
    data = _nullify_marker(
        data,
        b'className:"mb-8 min-w-0 gap-0"',
        "desktop token-source tabs",
    )

    # --- 8. Token 总览冗余标题 -------------------------------------------------
    data = _nullify_marker(
        data,
        b'id:"token-overview-title"',
        "redundant token-overview title",
    )

    # --- 9. CLI 专属的「查看请求明细」按钮 -------------------------------------
    data = _nullify_after(
        data,
        b'n==="codebuddy-cli"&&',
        "codebuddy-cli request-detail button",
    )

    # --- 10. 账号页「CodeBuddy CLI 接入」引导横幅 -------------------------------
    # 结构：``cond && p.jsxs(Bt,{className:"mb-4",children:[图标, 标题, 正文, 按钮]})``
    # 其中 cond = ``ie&&(!ie.configured||…||ie.syncPending)`` —— 依赖**官方后端状态**，
    # 只要后端返回 migrationRequired / syncPending / 未接入，横幅就会冒出来。
    # 容器里没有 CodeBuddy CLI，这个横幅永远不该出现，所以物理抹掉。
    #
    # marker 用带引号的完整标题（``"CodeBuddy CLI 接入"``）以保证唯一：
    # 另外两处同名字符串出现在 toast 文案 ``"CodeBuddy CLI 接入已更新"`` /
    # ``"CodeBuddy CLI 接入失败"`` 里，后面紧跟的是「已」/「失」而不是引号，不会误匹配。
    # 回溯两层：p.jsx(wa,{children:标题}) -> p.jsxs(Bt,{className:"mb-4",…})。
    data = _nullify_marker(
        data,
        b'"CodeBuddy CLI \xe6\x8e\xa5\xe5\x85\xa5"',
        "codebuddy-cli onboarding banner",
        levels=2,
    )

    # --- 11. 设置页：移除所有只对宿主桌面有意义的 section ----------------------
    # appearance(外观) 与 auto-checkin(自动签到) 是容器里真正可用的两项，保留。
    for section_id, label in (
        ("settings-auto-rotate", "settings: CodeBuddy CLI auto-rotate"),
        ("settings-permission", "settings: desktop permission check"),
        ("settings-rate-limit", "settings: desktop rate-limit monitor"),
        ("settings-startup", "settings: desktop startup/tray"),
        ("settings-updates", "settings: desktop auto-update"),
    ):
        data = _nullify_section(data, section_id, label)

    # --- 12. 设置页总描述文案 --------------------------------------------------
    def _build_subtitle(m):
        text = SETTINGS_SUBTITLE.encode("utf-8")
        inner = m.group(2)
        if len(text) > len(inner):
            return None
        return m.group(1) + text + b" " * (len(inner) - len(text)) + m.group(3)

    data = _replace_padded(
        data,
        rb'(text-muted-foreground",children:")(\xe8\x87\xaa\xe5\x8a\xa8\xe7\xad\xbe\xe5\x88\xb0[^"]*)(")',
        _build_subtitle,
        "settings page subtitle",
    )

    # --- 收尾校验 -------------------------------------------------------------
    if len(data) != size0:
        _fail("length invariant", "%d != %d" % (len(data), size0))
        return

    with open(bin_path, "wb") as f:
        f.write(data)
    print("patch complete: %d bytes (unchanged)" % len(data))


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: patch_binary.py <wb-switch-binary> [<binary> ...]")

    for path in argv[1:]:
        patch(path)
        print("")

    print("=" * 62)
    print("applied: %d   failed: %d" % (len(_APPLIED), len(_FAILED)))
    if _FAILED:
        print("")
        print("补丁失败项（官方前端结构可能已变更，请更新 patch/patch_binary.py 的锚点）：")
        for label, detail in _FAILED:
            print("  - %s: %s" % (label, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
