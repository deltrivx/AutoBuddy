"""容器内隔离验证：只抽 _backup_accounts / _prune_accounts_backups 两个函数，
喂临时目录的假权威源。绝不触碰 /data/.wb-switch/accounts.json。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="abprobe2-")
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/gateway")

src = open("/tmp/main_new.py", encoding="utf-8").read()
ns = {"os": os, "json": json, "time": time, "shutil": shutil, "Path": Path,
      "Optional": __import__("typing").Optional, "print": print}
# 只抽两个纯函数（不碰 _save_accounts，它内部有硬编码路径）
seg = src[src.index("_ACCOUNTS_BAK_KEEP = 20"):src.index("def _save_accounts(")]
exec(compile(seg, "probe", "exec"), ns)

primary = Path(TMP) / "accounts.json"
primary.write_text(json.dumps([{"id": "v0"}], ensure_ascii=False), encoding="utf-8")

print("== 连续 5 次留底（模拟 5 次账号变更）==")
for i in range(5):
    primary.write_text(json.dumps([{"id": f"v{i}"}], ensure_ascii=False), encoding="utf-8")
    p = ns["_backup_accounts"](primary)
    print(f"  第 {i+1} 次 -> {p.name if p else None}")

baks = sorted(primary.parent.glob("accounts.json.bak-*"))
print("备份数:", len(baks))
print("★ 每份备份应保存它那一轮的**前一个**内容（v0..v3），证明留底先于写入")
for b in baks:
    print("   ", b.name, "->", json.loads(b.read_text(encoding="utf-8")))

print("\n== 份数上限 ==")
print("上限:", ns["_ACCOUNTS_BAK_KEEP"])
for i in range(30):
    primary.write_text(json.dumps([{"id": f"x{i}"}], ensure_ascii=False), encoding="utf-8")
    ns["_backup_accounts"](primary)
baks = sorted(primary.parent.glob("accounts.json.bak-*"))
print("30 次后备份数:", len(baks), "(应 == 上限)")

print("\n== 无原文件时留底返回 None、不抛 ==")
gone = Path(TMP) / "nope.json"
print("返回:", ns["_backup_accounts"](gone))

shutil.rmtree(TMP, ignore_errors=True)
print("\n临时目录已清理，未触碰任何真实数据")
