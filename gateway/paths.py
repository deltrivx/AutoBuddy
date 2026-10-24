"""统一的路径解析：所有数据目录 / 官方镜像目录都必须从这里取。

为什么要有这个模块（2026-10-24 实测缺陷）：

代码里原本并存两种写法 —— 19 处用 ``Path(os.getenv("AB_DATA_DIR", ...))``
从环境变量读，8 处直接写死 ``/data/.autobuddy`` 或 ``/data/.wb-switch``。
当前恰好 ``AB_DATA_DIR == /data/.autobuddy`` 且 ``HOME == /data``，两边解析
结果相同，所以一直没有暴露。但只要把挂载点从 ``/data`` 换到别处：

- 那 19 处会正确跟着 ``AB_DATA_DIR`` 走；
- 那 8 处仍会往 ``/data/...`` 写 —— 而 ``/data`` 此时已不是挂载卷，
  新文件落进**容器可写层**，容器一重建就全部丢失，且**不会有任何报错**。

其中 ``token_tracker`` 最严重：它用 ``TRACKER_FILE.parent`` 决定
``token_stats.db``（5MB+ 用量库）的位置，等于绕过了 ``AB_DATA_DIR``。

所以把两类路径集中到这里，其余模块一律调用本模块，不再出现裸路径字符串。
"""

import os
from pathlib import Path

# 兼容两种导入方式（2026-10-24 实测踩过）：
#   1. ``import paths``           —— 当 gateway/ 在 sys.path 上时（web_proxy 等模块级脚本）
#   2. ``from gateway import ...`` —— 当调用方把仓库根加进 sys.path 时
# 本模块自身不 import 任何自定义模块，所以两种方式都能工作；
# 调用方统一用 ``from paths import ...`` 即可，因为 gateway/ 总会
# 通过 PYTHONPATH 或 sys.path.insert 出现在搜索路径上（现有代码已如此：
# wb_daily.py / web_proxy.py 都是裸 ``import db``）。

# 数据目录的兜底值。与 Dockerfile 的 ENV AB_DATA_DIR 保持一致。
_DEFAULT_DATA_DIR = "/data/.autobuddy"

# 官方底层二进制的 HOME。与 Dockerfile 的 ENV HOME 保持一致。
_DEFAULT_HOME = "/data"

# 官方 wb-switch 二进制**硬编码**读取 $HOME/.wb-switch（见 entrypoint.sh 注释），
# 这个目录名由官方二进制决定，不是我们的配置项，所以不做成独立环境变量。
# 推导方式必须是「HOME + 固定子目录名」，而不是再引入一个 env —— 否则
# HOME 改了而新 env 没改，又会造出同样的不一致。
_SWITCH_SUBDIR = ".wb-switch"


def get_data_dir() -> Path:
    """数据目录。

    可用 ``AB_DATA_DIR`` 覆盖，默认 ``/data/.autobuddy``。
    所有需要读写 accounts.json / autobuddy.db / token_stats.db 等文件的地方，
    都必须经由这里（或 ``db.DATA_DIR``，它内部调用的也是同一份约定）。
    """
    return Path(os.getenv("AB_DATA_DIR") or _DEFAULT_DATA_DIR)


def get_home_dir() -> Path:
    """容器 HOME 目录。可用 ``HOME`` 覆盖，默认 ``/data``。"""
    return Path(os.getenv("HOME") or _DEFAULT_HOME)


def get_switch_dir() -> Path:
    """官方服务镜像目录（wb-switch 二进制读写的那个）。

    必须由 ``$HOME/.wb-switch`` 派生 —— 官方二进制就是这么找的，
    我们无法改它的查找规则，只能跟着 HOME 走。

    注意：它与 ``get_data_dir()`` 默认不是同一层目录（``/data/.wb-switch``
    vs ``/data/.autobuddy``），二者由 ``entrypoint.sh`` 建立双向软链互通。
    """
    return get_home_dir() / _SWITCH_SUBDIR


def get_switch_file(name: str) -> Path:
    """官方镜像目录下的某个文件。``get_switch_file("accounts.json")``。"""
    return get_switch_dir() / name


def get_data_file(name: str) -> Path:
    """数据目录下的某个文件。``get_data_file("accounts.json")``。"""
    return get_data_dir() / name
