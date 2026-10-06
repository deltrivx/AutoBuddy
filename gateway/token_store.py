"""Token 用量的 SQLite 存储层。

为什么要有这个模块（用户 2026-10-02 授权迁移）：

原先用量明细是**单个 JSON 文件**，每次记录都要「读整个文件 → append →
写整个文件」。窗口写满 3000 条时约 1.28MB 一轮 I/O，请求一密就把磁盘打满。
v0.9.27 用攒批处理过（内存攒批 + 到量/到点落盘），但**每次落盘仍然是
整文件重写** —— 攒批只降低了频率，没改变单次成本。

参考 sub2api（43k stars）的做法：明细落到数据库表，写入是**增量 INSERT**
（`ON CONFLICT ... DO NOTHING` 幂等），并建复合索引服务聚合查询。

设计取舍（重要）：

本模块**只负责存取，不负责聚合**。聚合逻辑仍在 ``token_tracker`` 里，
一行未改。这样：

- 输出契约（daily/models/projects/dailyByModel/requests/sessions/summary）
  **零风险**保持原样，现有 68 项测试能继续验证语义；
- 真正的瓶颈在写入（每请求整文件重写），换成 INSERT 后即解决；
- 读取侧明细受时间保留约束（默认 7 天，约 1.4 万条），
  Python 侧聚合够用，不必为了 SQL 聚合重写整套数值语义。

如果将来明细量再涨一个数量级，可以把聚合也下推到 SQL —— 届时本模块
加聚合函数即可，上层不用动。
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))

# 数据库文件。测试会把它指到临时目录，与 token_tracker 的 TRACKER_FILE
# 一样是模块级可变配置。
DB_FILE = DATA_DIR / "token_stats.db"

# 明细/聚合的保留天数，由 token_tracker 在初始化时同步过来（避免两处配置漂移）
DETAIL_MAX_DAYS = int(os.getenv("AB_TOKEN_DETAIL_DAYS", "7") or 7)
ROLLUP_MAX_DAYS = int(os.getenv("AB_TOKEN_ROLLUP_DAYS", "90") or 90)

# SQLite 是单写者模型：用一把锁 + WAL 让并发读写都不互相阻塞。
# WAL 下读不阻塞写、写不阻塞读，这对「请求线程写 + 统计页读」很关键。
_WRITE_LOCK = threading.Lock()
_INIT_DONE = False
_INIT_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE), timeout=15.0)
    conn.row_factory = sqlite3.Row
    # WAL：统计页读取不被写入阻塞；busy_timeout 防并发写直接抛错
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def configure(db_file) -> None:
    """切换数据库文件（带变化检测，重复调用几乎无开销）。

    为什么需要：测试会把 ``token_tracker.TRACKER_FILE`` 指到临时目录，
    数据库必须跟随同一目录，否则测试会写进生产库。
    路径没变时直接返回，不重复建表。
    """
    global DB_FILE, _INIT_DONE
    p = Path(db_file)
    if DB_FILE == p and _INIT_DONE:
        return
    DB_FILE = p
    _INIT_DONE = False


def init_db() -> None:
    """建表与索引（进程内只做一次，幂等）。"""
    global _INIT_DONE
    with _INIT_LOCK:
        if _INIT_DONE:
            return
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_logs (
                    id            TEXT PRIMARY KEY,
                    ts            INTEGER NOT NULL,
                    time          TEXT NOT NULL DEFAULT '',
                    date          TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    input         INTEGER NOT NULL DEFAULT 0,
                    output        INTEGER NOT NULL DEFAULT 0,
                    cache_read    INTEGER NOT NULL DEFAULT 0,
                    cache_write   INTEGER NOT NULL DEFAULT 0,
                    uncached_input INTEGER NOT NULL DEFAULT 0,
                    duration      REAL NOT NULL DEFAULT 0,
                    account_id    TEXT NOT NULL DEFAULT '',
                    account_name  TEXT NOT NULL DEFAULT '',
                    variant       TEXT NOT NULL DEFAULT '',
                    source        TEXT NOT NULL DEFAULT 'gateway',
                    -- 等价美元成本（观感用）。NULL = 未定价，不是 0 ——
                    -- 0 表示「这个模型免费」，NULL 表示「没有定价数据」。
                    cost          REAL
                );

                -- 服务「按天 / 按模型 / 按账号」三类聚合查询，
                -- 对齐 sub2api 的复合索引思路（列顺序 = 查询条件顺序）
                CREATE INDEX IF NOT EXISTS idx_usage_logs_date
                    ON usage_logs(date);
                CREATE INDEX IF NOT EXISTS idx_usage_logs_model_date
                    ON usage_logs(model, date);
                CREATE INDEX IF NOT EXISTS idx_usage_logs_account_date
                    ON usage_logs(account_id, date);
                CREATE INDEX IF NOT EXISTS idx_usage_logs_variant_date
                    ON usage_logs(variant, date);
                CREATE INDEX IF NOT EXISTS idx_usage_logs_ts
                    ON usage_logs(ts DESC);

                CREATE TABLE IF NOT EXISTS usage_rollup (
                    date          TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    account_id    TEXT NOT NULL,
                    variant       TEXT NOT NULL,
                    account_name  TEXT NOT NULL DEFAULT '',
                    input         INTEGER NOT NULL DEFAULT 0,
                    output        INTEGER NOT NULL DEFAULT 0,
                    cache_read    INTEGER NOT NULL DEFAULT 0,
                    cache_write   INTEGER NOT NULL DEFAULT 0,
                    uncached_input INTEGER NOT NULL DEFAULT 0,
                    total         INTEGER NOT NULL DEFAULT 0,
                    records       INTEGER NOT NULL DEFAULT 0,
                    duration      REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (date, model, account_id, variant)
                );

                CREATE INDEX IF NOT EXISTS idx_usage_rollup_date
                    ON usage_rollup(date);

                -- 迁移标记：记录已从旧 JSON 导入，避免重复导入
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()
            # 老库升级：CREATE TABLE IF NOT EXISTS 对已存在的表不做任何事，
            # 后加的列必须在这里补。生产库 v0.9.29 建表时还没有 cost。
            _ensure_column(conn, "usage_logs", "cost", "REAL")
        finally:
            conn.close()
        _INIT_DONE = True


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                  decl: str) -> None:
    """幂等补列 —— 老库升级用。

    为什么必须有这一段：``CREATE TABLE IF NOT EXISTS`` 对**已存在的表**
    不会做任何事。生产库在 v0.9.29 就已建好 usage_logs，那时还没有
    cost 列；直接部署带 cost 的版本，写入/读取都会撞
    ``sqlite3.OperationalError: no such column: cost``。

    所以建表之后必须再逐列检查一次，缺了就 ALTER 补上。
    重复执行安全（列已存在时直接返回）。
    """
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except Exception:
        return
    if column in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
    except Exception as e:
        print(f"[token_store] add column {table}.{column} failed: {e}")


# ---------------------------------------------------------------------------
# 行 ↔ 字典 的字段映射
#
# SQLite 列用下划线命名（SQL 惯例），但上层的聚合逻辑读的是驼峰键
# （cacheRead / accountId ...）。这里统一做一次转换，让上层完全无感。
# ---------------------------------------------------------------------------

def _row_to_detail(row: sqlite3.Row) -> Dict[str, Any]:
    inp = int(row["input"] or 0)
    out = int(row["output"] or 0)
    cr = int(row["cache_read"] or 0)
    return {
        "id": row["id"],
        "time": row["time"] or "",
        "ts": int(row["ts"] or 0),
        "timestamp": int(row["ts"] or 0),
        "date": row["date"],
        "model": row["model"],
        "input": inp,
        "output": out,
        "cacheRead": cr,
        "cacheWrite": int(row["cache_write"] or 0),
        "uncachedInput": int(row["uncached_input"] or 0),
        "total": inp + out,
        "duration": float(row["duration"] or 0.0),
        "accountId": row["account_id"] or "",
        "accountName": row["account_name"] or "",
        "variant": row["variant"] or "",
        "source": row["source"] or "gateway",
        # 未定价读回来是 None（不是 0），与写入语义一致
        "cost": row["cost"],
    }


def _row_to_rollup(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "date": row["date"],
        "model": row["model"],
        "accountId": row["account_id"] or "",
        "variant": row["variant"] or "",
        "accountName": row["account_name"] or "",
        "input": int(row["input"] or 0),
        "output": int(row["output"] or 0),
        "cacheRead": int(row["cache_read"] or 0),
        "cacheWrite": int(row["cache_write"] or 0),
        "uncachedInput": int(row["uncached_input"] or 0),
        "total": int(row["total"] or 0),
        "records": int(row["records"] or 0),
        "duration": float(row["duration"] or 0.0),
    }


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def insert_batch(entries: List[Dict[str, Any]]) -> int:
    """批量写入明细行（幂等：同 request_id 重复写会被忽略）。

    用 ``INSERT OR IGNORE`` 而不是先查后写 —— sub2api 用的是
    ``ON CONFLICT (request_id, api_key_id) DO NOTHING``，语义相同：
    重试、重放、并发重复提交都不会产生重复计数。
    """
    if not entries:
        return 0
    init_db()
    rows = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        inp = int(e.get("input") or 0)
        out = int(e.get("output") or 0)
        rows.append((
            str(e.get("id") or ""),
            int(e.get("timestamp") or e.get("ts") or 0),
            str(e.get("time") or ""),
            str(e.get("date") or ""),
            str(e.get("model") or "unknown"),
            inp,
            out,
            int(e.get("cacheRead") or 0),
            int(e.get("cacheWrite") or 0),
            int(e.get("uncachedInput") or 0),
            float(e.get("duration") or 0.0),
            str(e.get("accountId") or ""),
            str(e.get("accountName") or ""),
            str(e.get("variant") or ""),
            str(e.get("source") or "gateway"),
            # 未定价模型写 None，不写 0（0 是「免费」，None 是「未知」）
            (float(e["cost"]) if isinstance(e.get("cost"), (int, float)) else None),
        ))
    with _WRITE_LOCK:
        conn = _connect()
        try:
            conn.executemany(
                """
                INSERT OR IGNORE INTO usage_logs
                    (id, ts, time, date, model, input, output, cache_read,
                     cache_write, uncached_input, duration, account_id,
                     account_name, variant, source, cost)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            conn.commit()
            return conn.total_changes
        finally:
            conn.close()


def fold_into_rollup(entries: List[Dict[str, Any]]) -> None:
    """把明细行折算成「天 × 模型 × 账号 × variant」的聚合行。

    用 UPSERT（``ON CONFLICT ... DO UPDATE``）累加，而不是读出来再写回 ——
    既避免读-改-写的竞态，也省掉一次全量读写。
    """
    if not entries:
        return
    init_db()
    with _WRITE_LOCK:
        conn = _connect()
        try:
            for e in entries:
                if not isinstance(e, dict):
                    continue
                inp = int(e.get("input") or 0)
                out = int(e.get("output") or 0)
                conn.execute(
                    """
                    INSERT INTO usage_rollup
                        (date, model, account_id, variant, account_name,
                         input, output, cache_read, cache_write,
                         uncached_input, total, records, duration)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(date, model, account_id, variant) DO UPDATE SET
                        input         = input + excluded.input,
                        output        = output + excluded.output,
                        cache_read    = cache_read + excluded.cache_read,
                        cache_write   = cache_write + excluded.cache_write,
                        uncached_input = uncached_input + excluded.uncached_input,
                        total         = total + excluded.total,
                        records       = records + excluded.records,
                        duration      = duration + excluded.duration,
                        account_name  = CASE
                            WHEN usage_rollup.account_name = ''
                             AND excluded.account_name <> ''
                            THEN excluded.account_name
                            ELSE usage_rollup.account_name END
                    """,
                    (
                        str(e.get("date") or ""),
                        str(e.get("model") or "unknown"),
                        str(e.get("accountId") or ""),
                        str(e.get("variant") or ""),
                        str(e.get("accountName") or ""),
                        inp,
                        out,
                        int(e.get("cacheRead") or 0),
                        int(e.get("cacheWrite") or 0),
                        int(e.get("uncachedInput") or 0),
                        inp + out,
                        # 明细行代表 1 次真实调用；若上游已是聚合行则按其 records
                        max(1, int(e.get("records") or 1)),
                        float(e.get("duration") or 0.0),
                    ),
                )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def fetch_detail(days: Optional[int] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """读取明细（默认只取保留期内）。

    行数护栏在这里退化为 ``limit``：明细已按天保留，跨度是确定的，
    limit 只在极端情况下防止一次拉回过多行。
    """
    init_db()
    if days is None:
        days = DETAIL_MAX_DAYS
    sql = "SELECT * FROM usage_logs"
    args: List[Any] = []
    if days and days > 0:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sql += " WHERE date >= ?"
        args.append(cutoff)
    sql += " ORDER BY ts DESC"
    if limit and limit > 0:
        sql += " LIMIT ?"
        args.append(int(limit))
    conn = _connect()
    try:
        return [_row_to_detail(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def fetch_rollup(days: Optional[int] = None) -> List[Dict[str, Any]]:
    """读取聚合行（默认只取保留期内）。"""
    init_db()
    if days is None:
        days = ROLLUP_MAX_DAYS
    sql = "SELECT * FROM usage_rollup"
    args: List[Any] = []
    if days and days > 0:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sql += " WHERE date >= ?"
        args.append(cutoff)
    conn = _connect()
    try:
        return [_row_to_rollup(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def count_detail() -> int:
    init_db()
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM usage_logs").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 保留策略（按时间裁剪，超期明细先折算进聚合）
# ---------------------------------------------------------------------------

def expire_detail(days: Optional[int] = None) -> int:
    """把超期明细折算进聚合后删除，返回处理条数。

    与 v0.9.28 的 ``_split_expired`` 语义一致：超期明细**不能直接丢**，
    先保住汇总数字再删明细。
    """
    init_db()
    if days is None:
        days = DETAIL_MAX_DAYS
    if not days or days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _connect()
    try:
        expired = [_row_to_detail(r) for r in conn.execute(
            "SELECT * FROM usage_logs WHERE date < ? AND date <> ''", (cutoff,))]
        if not expired:
            return 0
        conn.close()
        fold_into_rollup(expired)
        conn = _connect()
        cur = conn.execute(
            "DELETE FROM usage_logs WHERE date < ? AND date <> ''", (cutoff,))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def prune_rollup(days: Optional[int] = None) -> int:
    """删除超期聚合行（聚合本身就是汇总，删了不需要再折算）。"""
    init_db()
    if days is None:
        days = ROLLUP_MAX_DAYS
    if not days or days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM usage_rollup WHERE date < ? AND date <> ''", (cutoff,))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 从旧 JSON 迁移（一次性，线上数据不丢）
# ---------------------------------------------------------------------------

def migrate_from_json(detail_path: Optional[Path] = None,
                      rollup_path: Optional[Path] = None) -> Dict[str, int]:
    """把 v0.9.28 及更早的 JSON 明细/聚合导入 SQLite。

    为什么要这一步：线上已经有 3000 条明细和一份聚合，直接切存储
    会让历史数据凭空消失。迁移幂等（靠 meta 标记 + INSERT OR IGNORE），
    重复调用不会重复计数。
    """
    init_db()
    stats = {"detail": 0, "rollup": 0, "skipped": False}
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'json_migrated'").fetchone()
        if row is not None:
            stats["skipped"] = True
            return stats
    finally:
        conn.close()

    if detail_path and Path(detail_path).exists():
        try:
            with open(detail_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                # 必须按保留期分流，不能一股脑塞进明细：
                # 超出保留期的记录进了明细后，读取时会被时间窗口排除，
                # 而它们又没有被折算进聚合 —— 两头都不落地，等于**静默丢数据**。
                # 这正是升级时最容易踩的坑（旧的 3000 条里必然有超期的）。
                cutoff = ""
                if DETAIL_MAX_DAYS and DETAIL_MAX_DAYS > 0:
                    cutoff = (datetime.now() - timedelta(
                        days=DETAIL_MAX_DAYS)).strftime("%Y-%m-%d")
                fresh, stale = [], []
                for rec in data:
                    if not isinstance(rec, dict):
                        continue
                    d = str(rec.get("date") or "")
                    # 没有日期的一律当新鲜处理，宁可多留也不能误判成超期删掉
                    if cutoff and d and d < cutoff:
                        stale.append(rec)
                    else:
                        fresh.append(rec)
                if fresh:
                    stats["detail"] = insert_batch(fresh)
                if stale:
                    fold_into_rollup(stale)
                    stats["folded"] = len(stale)
        except Exception as e:
            print(f"[token_store] migrate detail failed: {e}")

    if rollup_path and Path(rollup_path).exists():
        try:
            with open(rollup_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                fold_into_rollup(data)
                stats["rollup"] = len(data)
        except Exception as e:
            print(f"[token_store] migrate rollup failed: {e}")

    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('json_migrated', ?)",
            (str(int(time.time())),),
        )
        conn.commit()
    finally:
        conn.close()
    return stats
