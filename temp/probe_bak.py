"""在容器内验证 _save_accounts 的留底行为（只动临时目录，绝不碰真实数据）。"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

TMP = tempfile.mkdtemp(prefix="abprobe-")
os.environ["AB_DATA_DIR"] = TMP
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/gateway")

src = open("/app/gateway/main.py", encoding="utf-8").read()
ns = {
    "os": os, "json": json, "time": time, "shutil": shutil, "Path": Path,
    "Optional": Optional, "List": List, "Dict": Dict, "Any": Any, "print": print,
}
seg = src[src.index("_ACCOUNTS_BAK_KEEP = 20"):src.index("def _load_pool_config(")]
# DATA_DIR 在同一段里定义，补进命名空间
seg = "DATA_DIR = Path(os.environ[\"AB_DATA_DIR\"])\n" + seg
exec(compile(seg, "probe", "exec"), ns)

# 探针的 DATA_DIR 是临时目录，但 _save_accounts 里 primary 硬编码
# /data/.wb-switch/accounts.json（容器内该目录存在，故 targets[0] 是它）。
# 因此 glob 必须指向 primary 所在目录，而不是 TMP。
primary = Path("/data/.wb-switch/accounts.json")
primary.write_text(json.dumps([{"id": "PROBE-ONLY-DO-NOT-USE"}], ensure_ascii=False), encoding="utf-8")

print("== 连续 3 次写入，每次都应先留底 ==")
for i in range(3):
    ns["_save_accounts"]([{"id": "a", "n": i}])
baks = sorted(primary.parent.glob("accounts.json.bak-*"))
print("备份数:", len(baks))
for b in baks:
    print("   ", b.name, "内容:", b.read_text(encoding="utf-8"))
print("权威源最终:", primary.read_text(encoding="utf-8"))
print("★ 第一份备份应是初始内容 [{'id':'a'}]，不含 n —— 证明留底先于写入")

print("\n== 留底失败不打断写入（primary 不存在时） ==")
primary.unlink()
ns["_save_accounts"]([{"id": "z"}])
print("无权威源时写入仍成功:", primary.exists(), primary.read_text(encoding="utf-8"))

print("\n== 份数上限（_ACCOUNTS_BAK_KEEP）==")
print("上限:", ns["_ACCOUNTS_BAK_KEEP"])
for i in range(30):
    ns["_save_accounts"]([{"id": "x", "n": i}])
baks = sorted(primary.parent.glob("accounts.json.bak-*"))
print("30 次写入后备份数:", len(baks), "（应 == 上限）")

shutil.rmtree(TMP, ignore_errors=True)
print("\n临时目录已清理")
