"""官方圆形头像 SVG。

官方 ``icon.png`` 是方形 App 图标，前端圆形硬裁会切坏内容、露出直角边缘。
这里在运行时把它内嵌进一个浅蓝纯圆底衬的正圆 SVG —— logo 居中、四周留白均匀，
既保持官方辨识度，又和预设图标一样是干净的圆形构图。

每次进程启动时读取 icon.png 生成，避免把 base64 静态写死（图标更新后自动跟随）。
"""
import base64
from pathlib import Path

_ICON_FILE = Path(__file__).resolve().parent.parent / "icon.png"


def _load_icon_b64() -> str:
    try:
        if _ICON_FILE.exists():
            return base64.b64encode(_ICON_FILE.read_bytes()).decode()
    except Exception:
        pass
    return ""


_ICON_B64 = _load_icon_b64()


def official_avatar_svg() -> str:
    """返回官方圆形头像的 SVG 字符串（每次调用重新取最新图标）。"""
    b64 = _load_icon_b64() or _ICON_B64
    if not b64:
        # 图标缺失时给一个纯底衬圆，绝不返回空内容导致前端破图
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<circle cx="32" cy="32" r="32" fill="#eff6ff"/></svg>'
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<circle cx="32" cy="32" r="32" fill="#eff6ff"/>'
        f'<image href="data:image/png;base64,{b64}" x="12" y="12" width="40" height="40"/>'
        '</svg>'
    )
