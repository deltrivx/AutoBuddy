"""本地单测：选账号流水落盘（v0.3.18）。

本机没有 fastapi / httpx，用 stub 顶掉后导入 gateway/main.py，
再验证 _load_selection_log / _save_selection_log_locked 的行为。

跑法：/usr/bin/python3 _test_selection_persist.py
"""
import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "gateway"))

FAILED = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


# ---------- stub fastapi / httpx ----------
class _App:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        def deco(*a, **k):
            if len(a) == 1 and callable(a[0]) and not k:
                return a[0]
            return lambda f: f
        return deco


class _HTTPException(Exception):
    def __init__(self, *a, **k):
        super().__init__(*a)


_fastapi = types.ModuleType("fastapi")
_fastapi.FastAPI = _App
_fastapi.HTTPException = _HTTPException
_fastapi.Request = type("Request", (), {})
_fastapi.Response = type("Response", (), {})
sys.modules["fastapi"] = _fastapi

_resp = types.ModuleType("fastapi.responses")
_resp.StreamingResponse = type("StreamingResponse", (), {})
sys.modules["fastapi.responses"] = _resp

_httpx = types.ModuleType("httpx")
_httpx.AsyncClient = type("AsyncClient", (), {})
sys.modules["httpx"] = _httpx

# ---------- 用临时目录当 DATA_DIR ----------
tmpdir = tempfile.mkdtemp(prefix="wbs-sel-")
os.environ["AB_DATA_DIR"] = tmpdir

main = importlib.import_module("main")
LOG = Path(tmpdir) / "selection_logs.json"

print("=== 1. 首次启动：无文件 -> 空流水，且不抛 ===")
check("初始 _SELECTION_LOG 为空", main._SELECTION_LOG == [], str(main._SELECTION_LOG))
check("此时磁盘无文件", not LOG.exists())

print("\n=== 2. 记录一次选择 -> 立即落盘 ===")
main._remember_selection({"id": "a1", "nickname": "账号甲"}, "auto")
check("内存 1 条", len(main._SELECTION_LOG) == 1)
check("磁盘文件已生成", LOG.exists())
on_disk = json.loads(LOG.read_text(encoding="utf-8"))
check("磁盘 1 条", len(on_disk) == 1, str(on_disk))
check("字段完整", set(on_disk[0]) >= {"ts", "accountId", "accountName", "source"}, str(on_disk[0]))
check("内容正确", on_disk[0]["accountId"] == "a1" and on_disk[0]["source"] == "auto")

print("\n=== 3. 模拟「容器重建」：重新导入模块 -> 从磁盘恢复 ===")
main2 = importlib.reload(main)
check("恢复后仍有 1 条", len(main2._SELECTION_LOG) == 1, str(main2._SELECTION_LOG))
check("恢复内容一致", main2._SELECTION_LOG[0]["accountId"] == "a1")

print("\n=== 4. 环形上限 200：写 250 条只保留最后 200 ===")
for i in range(250):
    main2._remember_selection({"id": f"x{i}", "nickname": f"N{i}"}, "auto")
check("内存被裁到 200", len(main2._SELECTION_LOG) == 200, str(len(main2._SELECTION_LOG)))
check("磁盘也被裁到 200", len(json.loads(LOG.read_text(encoding="utf-8"))) == 200)
check("保留的是最后 200 条", main2._SELECTION_LOG[-1]["accountId"] == "x249"
      and main2._SELECTION_LOG[0]["accountId"] == "x50",
      f"首={main2._SELECTION_LOG[0]['accountId']} 尾={main2._SELECTION_LOG[-1]['accountId']}")

print("\n=== 5. selection_stats 计数正确（滑动窗口语义）===")
st = main2.selection_stats(limit=3)
check("total == 200", st["total"] == 200, str(st["total"]))
check("recent 取 3 条", len(st["recent"]) == 3)
total_cnt = sum(c["count"] for c in st["counts"])
check("各账号计数之和 == total", total_cnt == 200, str(total_cnt))

print("\n=== 6. reset 同时清内存与磁盘 ===")
r = main2.reset_account_pool_selections()
check("返回 total 0", r.get("total") == 0, str(r))
check("内存已空", main2._SELECTION_LOG == [])
check("磁盘已空", json.loads(LOG.read_text(encoding="utf-8")) == [])
main3 = importlib.reload(main2)
check("重启后仍是空（清零不会被读回来）", main3._SELECTION_LOG == [], str(main3._SELECTION_LOG))

print("\n=== 7. 文件损坏 -> 返回空，不抛 ===")
LOG.write_text("{ this is not json", encoding="utf-8")
main4 = importlib.reload(main3)
check("损坏文件被容忍", main4._SELECTION_LOG == [], str(main4._SELECTION_LOG))

print("\n=== 8. 文件是合法 JSON 但结构不对 -> 返回空 ===")
LOG.write_text('{"not": "a list"}', encoding="utf-8")
main5 = importlib.reload(main4)
check("非 list 被容忍", main5._SELECTION_LOG == [])

LOG.write_text('[{"foo": 1}, {"accountId": "ok", "accountName": "B", "ts": 1}]', encoding="utf-8")
main6 = importlib.reload(main5)
check("过滤掉缺 accountId 的条目", len(main6._SELECTION_LOG) == 1
      and main6._SELECTION_LOG[0]["accountId"] == "ok", str(main6._SELECTION_LOG))

print("\n=== 9. 写盘失败不影响请求路径 ===")
import builtins
orig_open = builtins.open


def boom(*a, **k):
    raise OSError("disk full (simulated)")


builtins.open = boom
try:
    main6._remember_selection({"id": "z1", "nickname": "Z"}, "auto")
    check("写盘失败时 _remember_selection 仍正常返回", len(main6._SELECTION_LOG) == 2,
          str(len(main6._SELECTION_LOG)))
finally:
    builtins.open = orig_open
check("恢复后可正常落盘", (main6._remember_selection({"id": "z2", "nickname": "Z2"}, "auto") or True)
      and len(json.loads(LOG.read_text(encoding="utf-8"))) >= 2)

print("\n=== 10. 无残留 .tmp 文件 ===")
leftover = list(Path(tmpdir).glob("*.tmp"))
check("没有遗留 tmp", not leftover, str(leftover))

print("\n" + ("全部通过 ✅" if not FAILED else f"失败 {len(FAILED)} 项 ❌: {FAILED}"))
sys.exit(1 if FAILED else 0)
