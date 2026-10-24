消除 8 处硬编码路径，统一由 `gateway/paths.py` 按 `AB_DATA_DIR` 与 `HOME` 解析。

## 一、问题：8 处硬编码，换挂载点会静默丢数据

代码里并存两种写法：

| 写法 | 数量 | 行为 |
| :--- | :--- | :--- |
| `Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))` | **19 处** | ✅ 跟环境变量走 |
| `Path("/data/.autobuddy/...")` / `Path("/data/.wb-switch/...")` | **8 处** | ❌ 写死 |

当前恰好 `AB_DATA_DIR == /data/.autobuddy` 且 `HOME == /data`，两边解析结果相同，
所以从未暴露。但只要把挂载点从 `/data` 换到别处：

- 那 19 处正确跟着 `AB_DATA_DIR` 走；
- 那 8 处仍往 `/data/...` 写 —— 而 `/data` 此时已不是挂载卷，
  新文件落进**容器可写层，重建即丢，且不会有任何报错**。

### 完整的 8 处清单

| # | 文件:行号 | 原写法 |
| :--- | :--- | :--- |
| 1 | `gateway/token_tracker.py:43` | `TRACKER_FILE = Path("/data/.autobuddy/token_stats_logs.json")` ⚠️ |
| 2 | `gateway/credit_floor.py:55` | `Path("/data/.wb-switch/credit_usage_snapshots.json")` |
| 3 | `gateway/main.py:1236` | `primary = Path("/data/.wb-switch/accounts.json")` |
| 4 | `gateway/main.py:4171` | `sources = [Path("/data/.wb-switch/accounts.json"), ...]` |
| 5 | `gateway/wb_daily.py:61` | `ACCOUNTS_JSON = Path("/data/.wb-switch/accounts.json")` |
| 6 | `gateway/web_proxy.py:11142` | `Path("/data/.wb-switch/credit_usage_snapshots.json")` |
| 7 | `gateway/web_proxy.py:11363` | `Path("/data/.wb-switch/accounts.json")` |
| 8 | `gateway/web_proxy.py:12550` | `Path("/data/.wb-switch/accounts.json")` |

**`token_tracker.py` 最严重**：它下面两处以 `TRACKER_FILE.parent` 锚定位置 ——

```python
token_store.configure(TRACKER_FILE.parent / "token_stats.db")
def _rollup_path(): return TRACKER_FILE.parent / "token_stats_rollup.json"
```

也就是 5MB+ 的用量数据库位置由这个硬编码路径决定，**绕过了 `AB_DATA_DIR`**。

## 二、修法：统一到 `gateway/paths.py`

新增模块，提供两个入口：

```python
def get_data_dir() -> Path:
    """数据目录：AB_DATA_DIR，兜底 /data/.autobuddy"""
    return Path(os.getenv("AB_DATA_DIR") or "/data/.autobuddy")

def get_home_dir() -> Path:
    """容器 HOME：HOME，兜底 /data"""
    return Path(os.getenv("HOME") or "/data")

def get_switch_dir() -> Path:
    """官方镜像目录：由 $HOME/.wb-switch 派生"""
    return get_home_dir() / ".wb-switch"
```

外加 `get_data_file(name)` / `get_switch_file(name)` 两个便利函数。

### 为什么官方目录由 `HOME` 派生，而不是新增环境变量

`entrypoint.sh` 的注释写得很清楚：

> 官方底层 wb-switch 二进制**硬编码读取 `$HOME/.wb-switch/accounts.json`**。

这个子目录名由**官方闭源二进制**决定，我们无法改它的查找规则，只能跟着 `HOME` 走。
如果再引入一个独立的 `AB_SWITCH_DIR`，就会出现「`HOME` 改了而新 env 没改」
的新不一致 —— 正是这次要消除的同类问题。

所以：**数据目录用 `AB_DATA_DIR`（我们的配置项），官方目录用 `HOME` 派生（官方的约定）**，
两者层级不同、来源不同，各归其位。

### `entrypoint.sh` 同步变量化

```diff
-mkdir -p /data/.autobuddy
-mkdir -p /data/.wb-switch
+AB_DATA_DIR="${AB_DATA_DIR:-/data/.autobuddy}"
+AB_HOME="${HOME:-/data}"
+mkdir -p "$AB_DATA_DIR"
+mkdir -p "$AB_HOME/.wb-switch"

-for f in /data/.autobuddy/*; do
+for f in "$AB_DATA_DIR"/*; do
```

否则改挂载点后 Python 跟着新路径走、shell 还在建旧路径，两边打架。

## 三、行为零变化（已四场景验证）

| 场景 | `get_data_dir()` | `get_switch_dir()` |
| :--- | :--- | :--- |
| 不设任何环境变量 | `/data/.autobuddy` | `/data/.wb-switch` |
| 只设 `AB_DATA_DIR=/config/.autobuddy` | `/config/.autobuddy` | `/data/.wb-switch` |
| 只设 `HOME=/config` | `/data/.autobuddy` | `/config/.wb-switch` |
| **生产现状**（`AB_DATA_DIR=/data/.autobuddy`, `HOME=/data`） | `/data/.autobuddy` | `/data/.wb-switch` |

**生产现状那一行与修复前的硬编码值逐字符相同** —— 这是本次改动最重要的保证。

## 四、新增测试

`tests/test_paths_resolve.py`，25 项断言：

1. 兜底行为（不设任何 env）
2. `AB_DATA_DIR` 单设时的跟随
3. `HOME` 单设时的跟随
4. 两者都设时各自独立跟随
5. 生产现状复现
6. **源码扫描**：5 个目标文件不得再出现 `Path("/data/...")` 裸路径（防退化）
7. 工具模块存在且导出必要函数

第 6 项是关键 —— 它会在任何人把裸路径写回来时立刻红。

## 五、附带修复

`tests/test_entrypoint_symlink.py` 原本用 `str.replace("/data/.autobuddy", 沙箱路径)`
来构造沙箱。`entrypoint.sh` 变量化后，脚本里不再有那个字面量，替换失效、
链接一条都建不出来。现改为**注入 `AB_DATA_DIR` / `HOME` 环境变量**驱动，
验证的是真实的变量解析逻辑，比原来更贴近生产行为。
