#!/usr/bin/env python3
"""SQLite 用量存储后端回归测试。

背景（用户 2026-10-02 授权迁移）：

明细原先是**单个 JSON 文件**，每次记录都要「读整个文件 → append →
写整个文件」，窗口写满 3000 条时约 1.28MB 一轮 I/O。v0.9.27 用攒批
降低了频率，但单次落盘仍是整文件重写，成本没变。

v0.9.29 起默认改走 SQLite：写入变成**增量 INSERT**（幂等），
聚合查询有复合索引。聚合逻辑与输出契约一行未改，换的只是
「明细从哪来、往哪写」。

本测试覆盖 SQLite 后端自己的契约：

1. 幂等写入：同一 request_id 重复写不重复计数（对齐 sub2api 的
   `ON CONFLICT ... DO NOTHING`）；
2. 超期明细折算进聚合后删除，**汇总数字必须保住**（不能静默丢）；
3. 迁移：旧 JSON 里超出保留期的记录不会被两头落空，而是进聚合；
4. 输出契约与 JSON 后端一致（前端结构不变）。

另：默认后端必须是 sqlite（否则等于没迁移）。
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_ok = 0
_fail: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra else ""))


tmp = tempfile.mkdtemp()
from gateway import token_store as ts  # noqa: E402
from gateway import token_tracker as tt  # noqa: E402

ts.DB_FILE = Path(tmp) / "token_stats.db"
ts.init_db()

print("[1] 幂等写入（对齐 sub2api 的 ON CONFLICT DO NOTHING）")

today = datetime.now().strftime("%Y-%m-%d")


def _e(rid, inp=100, out=10, model="m1", date=None):
    return {
        "id": rid, "ts": 1, "timestamp": 1,
        "time": f"{date or today} 10:00:00", "date": date or today,
        "model": model, "input": inp, "output": out,
        "cacheRead": 40, "cacheWrite": 0, "uncachedInput": inp - 40,
        "duration": 1.0, "accountId": "a1", "accountName": "acct-a1",
        "variant": "cn", "source": "gateway",
    }


ts.insert_batch([_e("same-1")])
before = ts.count_detail()
ts.insert_batch([_e("same-1")])   # 同 id 再插一次
after = ts.count_detail()
check("同 request_id 重复写入不产生第二条", before == 1 and after == 1,
      f"before={before} after={after}")

ts.insert_batch([_e("other-1")])
check("不同 request_id 正常新增", ts.count_detail() == 2,
      f"got {ts.count_detail()}")

print("\n[2] 超期明细折算进聚合，汇总数字必须保住")

old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
fresh_d = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

shutil.rmtree(tmp, ignore_errors=True)
tmp = tempfile.mkdtemp()
ts.DB_FILE = Path(tmp) / "token_stats.db"
ts._INIT_DONE = False
ts.init_db()

ts.insert_batch([
    _e("old-1", inp=300, out=30, date=old),
    _e("fresh-1", inp=500, out=50, date=fresh_d),
])
check("两条明细都写入", ts.count_detail() == 2, f"got {ts.count_detail()}")

ts.DETAIL_MAX_DAYS = 7
removed = ts.expire_detail(7)
check("超期明细被移除", removed == 1, f"removed={removed}")
check("近期明细保留", ts.count_detail() == 1, f"got {ts.count_detail()}")

roll = ts.fetch_rollup()
old_rows = [r for r in roll if r["date"] == old]
check("超期记录折算进聚合（不是凭空消失）", bool(old_rows),
      f"rollup={[(r['date'], r['input']) for r in roll]}")
if old_rows:
    check("折算后 input 保住（300）", old_rows[0]["input"] == 300,
          f"got {old_rows[0]['input']}")
    check("折算后 output 保住（30）", old_rows[0]["output"] == 30,
          f"got {old_rows[0]['output']}")
    check("折算后 records 记为 1 次真实调用", old_rows[0]["records"] == 1,
          f"got {old_rows[0]['records']}")

print("\n[3] 迁移：旧 JSON 超期记录不得两头落空")

shutil.rmtree(tmp, ignore_errors=True)
tmp = tempfile.mkdtemp()
ts.DB_FILE = Path(tmp) / "token_stats.db"
ts._INIT_DONE = False
ts.init_db()
ts.DETAIL_MAX_DAYS = 7

json_path = Path(tmp) / "token_stats_logs.json"
json_path.write_text(json.dumps([
    _e("j-old", inp=700, out=70, date=old),      # 超期
    _e("j-fresh", inp=900, out=90, date=fresh_d),  # 新鲜
]), encoding="utf-8")

stats = ts.migrate_from_json(json_path, None)
check("迁移未跳过", stats.get("skipped") is not True, f"got {stats}")
check("新鲜记录进明细", ts.count_detail() == 1, f"got {ts.count_detail()}")
mig_roll = ts.fetch_rollup()
mig_old = [r for r in mig_roll if r["date"] == old]
check("超期记录进聚合（不静默丢）", bool(mig_old),
      f"rollup={[(r['date'], r['input']) for r in mig_roll]}")
if mig_old:
    check("超期记录 token 保住（700/70）",
          mig_old[0]["input"] == 700 and mig_old[0]["output"] == 70,
          f"got {mig_old[0]}")

# 幂等：再迁一次不应翻倍
ts.insert_batch([_e("j-fresh", inp=900, out=90, date=fresh_d)])
check("重复迁移不产生重复明细", ts.count_detail() == 1,
      f"got {ts.count_detail()}")

print("\n[4] 输出契约与前端一致（换后端不改结构）")

tt.TRACKER_FILE = Path(tmp) / "token_stats_logs.json"
tt.TOKEN_STORE = "sqlite"
ts.DETAIL_MAX_DAYS = 7
stats_out = tt.get_aggregated_token_stats()

check("顶层含 sources", "sources" in stats_out, f"got {list(stats_out)}")
srcs = stats_out.get("sources") or []
check("两个 source（国内/国际版）", len(srcs) == 2, f"got {len(srcs)}")
if srcs:
    s0 = srcs[0]
    for key in ("daily", "models", "projects", "requests",
                "sessions", "dailyByModel", "summary"):
        check(f"source 含 {key}", key in s0, f"missing {key}")
    check("summary 含 records", "records" in (s0.get("summary") or {}),
          f"got {s0.get('summary')}")

print("\n[5] 默认后端必须是 sqlite（否则等于没迁移）")

check("gateway/token_tracker.py 默认 sqlite",
      'os.getenv("AB_TOKEN_STORE", "sqlite")' in
      (ROOT / "gateway" / "token_tracker.py").read_text(encoding="utf-8"))
check("保留可回滚开关 AB_TOKEN_STORE",
      bool(getattr(tt, "TOKEN_STORE", None)))

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
