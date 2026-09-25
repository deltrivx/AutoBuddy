"""内置 SQLite 数据库管理模块 (AutoBuddy Database)。

持久化路径：/data/.autobuddy/autobuddy.db
职责：
1. WebUI 用户认证与会话管理（防未授权访问，支持密码修改与退出）。
2. 系统与自动化注册配置持久化存储（消除对容器环境变量的冗余依赖）。
3. 挂载在持久化卷 /data 下，容器升级/重建数据永不丢失。
"""

import sqlite3
import os
import hashlib
import secrets
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
DB_PATH = DATA_DIR / "autobuddy.db"


def get_db_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初始化数据库表结构与默认管理员用户。"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 1. 用户表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """)
        
        # 2. 会话 Token 表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """)
        
        # 3. 系统配置键值表 (统一持久化)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            description TEXT,
            updated_at REAL NOT NULL
        )
        """)

        # 4. 每日成长任务执行记录（拿每日任务面板用）。
        #
        # 为什么落库而不是只存内存：任务一轮可能跑十几分钟，容器一重启
        # 内存记录就没了，用户回头想查「昨天那个账号到底签到没」无从下手。
        # 只保留最新 N 条（见 prune_wb_daily_runs），避免无限增长。
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS wb_daily_runs (
            job_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL,
            accounts INTEGER NOT NULL DEFAULT 0,
            summary TEXT,
            log TEXT
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wb_daily_started ON wb_daily_runs(started_at DESC)")

        # 5. 逐账号执行明细（每个账号每轮一行）。
        # 面板要回答「每个账号执行了哪些」——汇总行里只有总数，
        # 无法拆到账号维度，所以单独一张表。
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS wb_daily_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            account TEXT NOT NULL,
            status TEXT,
            level TEXT,
            streak TEXT,
            energy TEXT,
            credits TEXT,
            usage TEXT,
            done_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            rest TEXT,
            actions TEXT,
            created_at REAL NOT NULL
        )""");
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wb_daily_acc_job ON wb_daily_accounts(job_id)")

        # 6. 任务台账（每个账号 × 每个任务项 × 每次签到/领奖一行）。
        # 面板要回答「这个任务今天做了几次、最后什么时候成功」——
        # 逐账号明细只存“完成 N/M”总数，拆不到任务维度，所以单独一张表。
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS wb_daily_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            account TEXT NOT NULL,
            task_key TEXT NOT NULL,
            task_name TEXT,
            group_name TEXT,
            result TEXT,
            detail TEXT,
            checked_in_dates TEXT,
            streak_days INTEGER,
            total_credits INTEGER,
            occurred_at REAL NOT NULL
        )""")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wb_daily_task_job ON wb_daily_tasks(job_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wb_daily_task_key ON wb_daily_tasks(task_key, occurred_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wb_daily_task_acct ON wb_daily_tasks(account, occurred_at DESC)");

        # 表结构平滑迁移：补齐 actions 列（如果旧库缺少）
        try:
            cursor.execute("PRAGMA table_info(wb_daily_accounts)")
            cols = [c[1] for c in cursor.fetchall()]
            if "actions" not in cols:
                cursor.execute("ALTER TABLE wb_daily_accounts ADD COLUMN actions TEXT")
        except Exception as e:
            print(f"[Database] wb_daily_accounts 表结构迁移失败: {e}")
        
        # 认证用户与环境变量动态同步：
        # 支持环境变量 AUTH_USERNAME / AUTH_USER / AUTH_DEFAULT_USER
        # 与 AUTH_PASSWORD / AUTH_PASS / AUTH_DEFAULT_PASS。
        # 未配置环境变量时，默认用户名 admin、默认密码 password。
        env_user = (os.getenv("AUTH_USERNAME") or os.getenv("AUTH_USER") or os.getenv("AUTH_DEFAULT_USER") or "admin").strip()
        env_pwd = (os.getenv("AUTH_PASSWORD") or os.getenv("AUTH_PASS") or os.getenv("AUTH_DEFAULT_PASS") or "password").strip()

        cursor.execute("SELECT id, username, salt FROM users WHERE username = ?", (env_user,))
        user_row = cursor.fetchone()
        now = time.time()

        if not user_row:
            # 用户不存在则创建（无论是默认 admin 还是环境变量指定的新用户名）
            salt = secrets.token_hex(16)
            pwd_hash = hashlib.sha256((env_pwd + salt).encode("utf-8")).hexdigest()
            cursor.execute(
                "INSERT INTO users (username, password_hash, salt, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (env_user, pwd_hash, salt, "admin", now, now)
            )
            print(f"[Database] 已初始化认证账号：{env_user}")
        else:
            # 若环境变量显式指定了密码，同步更新该账号密码
            if os.getenv("AUTH_PASSWORD") or os.getenv("AUTH_PASS") or os.getenv("AUTH_DEFAULT_PASS"):
                salt = user_row["salt"] or secrets.token_hex(16)
                pwd_hash = hashlib.sha256((env_pwd + salt).encode("utf-8")).hexdigest()
                cursor.execute(
                    "UPDATE users SET password_hash = ?, salt = ?, updated_at = ? WHERE id = ?",
                    (pwd_hash, salt, now, user_row["id"])
                )
                print(f"[Database] 环境变量显式指定密码，已同步更新账号：{env_user}")

        # 迁移既有的 gh_register_config.json 进数据库（如存在）
        cfg_file = DATA_DIR / "gh_register_config.json"
        if cfg_file.exists():
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    cfg_json = json.load(f)
                if isinstance(cfg_json, dict):
                    for k, v in cfg_json.items():
                        cursor.execute("SELECT key FROM system_config WHERE key = ?", (k,))
                        if not cursor.fetchone():
                            now = time.time()
                            val_str = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool, int, float)) else str(v)
                            cursor.execute(
                                "INSERT INTO system_config (key, value, category, updated_at) VALUES (?, ?, 'gh_register', ?)",
                                (k, val_str, now)
                            )
            except Exception as e:
                print(f"[Database] 迁移旧配置文件出错: {e}")

        conn.commit()


# ---------------- 认证与用户管理 ----------------

def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()


def verify_user(username: str, password: str) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash, salt FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if not row:
            return False
        return hash_password(password, row["salt"]) == row["password_hash"]


def create_session(username: str, duration_hours: int = 168) -> str:
    """创建会话，默认 7 天有效期。"""
    token = secrets.token_hex(32)
    now = time.time()
    expires_at = now + (duration_hours * 3600)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # 清理已过期会话
        cursor.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        cursor.execute(
            "INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, username, now, expires_at)
        )
        conn.commit()
    return token


def validate_session(token: str) -> Optional[str]:
    if not token:
        return None
    now = time.time()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username, expires_at FROM sessions WHERE token = ?", (token,))
        row = cursor.fetchone()
        if row and row["expires_at"] > now:
            return row["username"]
    return None


def destroy_session(token: str) -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


def change_password(username: str, new_password: str) -> bool:
    salt = secrets.token_hex(16)
    pwd_hash = hash_password(new_password, salt)
    now = time.time()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET password_hash = ?, salt = ?, updated_at = ? WHERE username = ?",
            (pwd_hash, salt, now, username)
        )
        conn.commit()
        return cursor.rowcount > 0


# ---------------- 系统配置持久化 ----------------

def get_config(key: str, default: Any = None) -> Any:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM system_config WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row["value"])
                except Exception:
                    return row["value"]
    except Exception:
        pass
    return default


def get_all_config(category: Optional[str] = None) -> Dict[str, Any]:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if category:
                cursor.execute("SELECT key, value FROM system_config WHERE category = ?", (category,))
            else:
                cursor.execute("SELECT key, value FROM system_config")
            res = {}
            for row in cursor.fetchall():
                try:
                    res[row["key"]] = json.loads(row["value"])
                except Exception:
                    res[row["key"]] = row["value"]
            return res
    except Exception:
        return {}


def set_config(key: str, value: Any, category: str = "general", description: str = "") -> None:
    val_str = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool, int, float)) else str(value)
    now = time.time()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO system_config (key, value, category, description, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            category = excluded.category,
            description = CASE WHEN excluded.description != '' THEN excluded.description ELSE system_config.description END,
            updated_at = excluded.updated_at
        """, (key, val_str, category, description, now))
        conn.commit()


# ---------------------------------------------------------------------------
# 每日成长任务执行记录
# ---------------------------------------------------------------------------

WB_DAILY_RETAIN = 200   # 执行记录保留条数（用户定的 100–200 区间，取上限）


def save_wb_daily_run(job_id: str, mode: str, status: str, started_at: float,
                      finished_at: Optional[float], accounts: int,
                      summary: Any = None, log: Any = None,
                      account_rows: Optional[List[Dict[str, Any]]] = None,
                      task_rows: Optional[List[Dict[str, Any]]] = None) -> None:
    """落盘一轮每日任务（汇总行 + 逐账号明细），并顺手剪掉超出保留额的老记录。

    日志存全文（上限由调用方控制），因为面板要能回看失败的账号到底卡在哪。
    写入用 INSERT OR REPLACE：同一 job_id 会随状态推进多次落盘
    （开始时先记 running，结束时覆盖成最终结果）。
    """
    now = time.time()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR REPLACE INTO wb_daily_runs
                (job_id, mode, status, started_at, finished_at, accounts, summary, log)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(job_id), str(mode), str(status), float(started_at),
                  float(finished_at) if finished_at else None, int(accounts),
                  json.dumps(summary, ensure_ascii=False) if summary is not None else None,
                  json.dumps(log, ensure_ascii=False) if log is not None else None))
            if account_rows:
                cursor.execute("DELETE FROM wb_daily_accounts WHERE job_id = ?", (str(job_id),))
                for row in account_rows:
                    cursor.execute("""
                    INSERT INTO wb_daily_accounts
                        (job_id, account, status, level, streak, energy, credits, usage,
                         done_count, total_count, rest, actions, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (str(job_id), str(row.get("account") or ""), row.get("status"),
                          row.get("level"), row.get("streak"), row.get("energy"),
                          row.get("credits"), row.get("usage"),
                          int(row.get("done") or 0), int(row.get("total") or 0),
                          json.dumps(row.get("rest"), ensure_ascii=False) if row.get("rest") is not None else None,
                          json.dumps(row.get("actions"), ensure_ascii=False) if row.get("actions") is not None else None,
                          now))
            conn.commit()
        if task_rows:
            save_wb_daily_tasks(job_id, task_rows)
        prune_wb_daily_runs()
    except Exception as e:
        print(f"[wb-daily] 写执行记录出错: {e}")


def save_wb_daily_tasks(job_id: str, task_rows: Optional[List[Dict[str, Any]]]) -> None:
    """落盘一轮的任务台账（任务维度，供面板按任务聚合展示）。

    与 ``wb_daily_accounts`` 的区别：那边一行 = 一个账号；这里一行 = 一个
    账号的一个任务项。面板要展示「任务记录」就必须有任务维度，否则只能从
    日志文本反解，极度不可靠。
    """
    if not task_rows:
        return
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM wb_daily_tasks WHERE job_id = ?", (str(job_id),))
            now = time.time()
            for row in task_rows:
                cursor.execute("""
                INSERT INTO wb_daily_tasks
                    (job_id, account, task_key, task_name, group_name, result, detail,
                     checked_in_dates, streak_days, total_credits, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(job_id), str(row.get("account") or ""),
                    str(row.get("task_key") or ""), row.get("task_name"),
                    row.get("group_name"), row.get("result"), row.get("detail"),
                    json.dumps(row.get("checked_in_dates"), ensure_ascii=False)
                    if row.get("checked_in_dates") is not None else None,
                    int(row["streak_days"]) if row.get("streak_days") is not None else None,
                    int(row["total_credits"]) if row.get("total_credits") is not None else None,
                    float(row.get("occurred_at") or now),
                ))
            conn.commit()
    except Exception as e:
        print(f"[wb-daily] 写任务台账出错: {e}")


def list_wb_daily_tasks(limit: int = 200, account: Optional[str] = None) -> List[Dict[str, Any]]:
    """取最近的任务台账（可按账号过滤），按时间倒序。"""
    limit = max(1, min(int(limit or 200), 1000))
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if account:
                cursor.execute("""
                SELECT job_id, account, task_key, task_name, group_name, result, detail,
                       checked_in_dates, streak_days, total_credits, occurred_at
                FROM wb_daily_tasks WHERE account = ?
                ORDER BY occurred_at DESC LIMIT ?
                """, (str(account), limit))
            else:
                cursor.execute("""
                SELECT job_id, account, task_key, task_name, group_name, result, detail,
                       checked_in_dates, streak_days, total_credits, occurred_at
                FROM wb_daily_tasks
                ORDER BY occurred_at DESC LIMIT ?
                """, (limit,))
            out = []
            for row in cursor.fetchall():
                dates = []
                try:
                    dates = json.loads(row["checked_in_dates"]) if row["checked_in_dates"] else []
                except Exception:
                    dates = []
                out.append({
                    "job_id": row["job_id"],
                    "account": row["account"],
                    "task_key": row["task_key"],
                    "task_name": row["task_name"],
                    "group_name": row["group_name"],
                    "result": row["result"],
                    "detail": row["detail"],
                    "checked_in_dates": dates,
                    "streak_days": row["streak_days"],
                    "total_credits": row["total_credits"],
                    "occurred_at": row["occurred_at"],
                })
            return out
    except Exception as e:
        print(f"[wb-daily] 读任务台账出错: {e}")
        return []


def summarize_wb_daily_tasks() -> List[Dict[str, Any]]:
    """按任务项聚合最近状态：每个任务最近一次结果 + 成功次数。

    面板「任务记录」卡片就靠它 —— 一眼看出哪些任务今天已做、哪些还没动静。
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT task_key, task_name, group_name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END) AS success,
                   MAX(occurred_at) AS last_at
            FROM wb_daily_tasks
            GROUP BY task_key
            ORDER BY last_at DESC
            """)
            out = []
            for row in cursor.fetchall():
                cursor.execute("""
                SELECT account, result, detail, occurred_at, streak_days, total_credits
                FROM wb_daily_tasks WHERE task_key = ?
                ORDER BY occurred_at DESC LIMIT 1
                """, (row["task_key"],))
                last = cursor.fetchone()
                out.append({
                    "task_key": row["task_key"],
                    "task_name": row["task_name"],
                    "group_name": row["group_name"],
                    "total": row["total"],
                    "success": row["success"] or 0,
                    "last_at": row["last_at"],
                    "last_account": last["account"] if last else None,
                    "last_result": last["result"] if last else None,
                    "last_detail": last["detail"] if last else None,
                    "streak_days": last["streak_days"] if last else None,
                    "total_credits": last["total_credits"] if last else None,
                })
            return out
    except Exception as e:
        print(f"[wb-daily] 聚合任务台账出错: {e}")
        return []


def prune_wb_daily_runs(keep: int = WB_DAILY_RETAIN) -> int:
    """只保留最新 `keep` 轮记录，返回删掉的轮数。

    按 started_at 倒序取第 keep 条之后的全删，并同步清掉它们的逐账号明细。
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT job_id FROM wb_daily_runs ORDER BY started_at DESC LIMIT -1 OFFSET ?
            """, (int(keep),))
            stale = [row["job_id"] for row in cursor.fetchall()]
            if not stale:
                return 0
            marks = ",".join("?" * len(stale))
            cursor.execute(f"DELETE FROM wb_daily_accounts WHERE job_id IN ({marks})", stale)
            cursor.execute(f"DELETE FROM wb_daily_tasks WHERE job_id IN ({marks})", stale)
            cursor.execute(f"DELETE FROM wb_daily_runs WHERE job_id IN ({marks})", stale)
            conn.commit()
            return len(stale)
    except Exception as e:
        print(f"[wb-daily] 剪枝执行记录出错: {e}")
        return 0


def list_wb_daily_runs(limit: int = 20) -> List[Dict[str, Any]]:
    """取最近几轮执行记录（不含逐账号明细，明细走 get_wb_daily_accounts）。"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT job_id, mode, status, started_at, finished_at, accounts, summary
            FROM wb_daily_runs ORDER BY started_at DESC LIMIT ?
            """, (int(limit),))
            out = []
            for row in cursor.fetchall():
                summary = None
                try:
                    summary = json.loads(row["summary"]) if row["summary"] else None
                except Exception:
                    summary = None
                out.append({
                    "job_id": row["job_id"],
                    "mode": row["mode"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "accounts": row["accounts"],
                    "summary": summary,
                })
            return out
    except Exception as e:
        print(f"[wb-daily] 读执行记录出错: {e}")
        return []


def get_wb_daily_accounts(job_id: str) -> List[Dict[str, Any]]:
    """取某一轮的逐账号明细（面板回答「每个账号执行了哪些」靠它）。"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT account, status, level, streak, energy, credits, usage,
                   done_count, total_count, rest, actions
            FROM wb_daily_accounts WHERE job_id = ? ORDER BY id
            """, (str(job_id),))
            out = []
            for row in cursor.fetchall():
                rest = []
                try:
                    rest = json.loads(row["rest"]) if row["rest"] else []
                except Exception:
                    rest = []
                actions = []
                try:
                    actions = json.loads(row["actions"]) if row["actions"] else []
                except Exception:
                    actions = []
                out.append({
                    "account": row["account"],
                    "status": row["status"],
                    "level": row["level"],
                    "streak": row["streak"],
                    "energy": row["energy"],
                    "credits": row["credits"],
                    "usage": row["usage"],
                    "done": row["done_count"],
                    "total": row["total_count"],
                    "rest": rest,
                    "actions": actions,
                })
            return out
    except Exception as e:
        print(f"[wb-daily] 读账号明细出错: {e}")
        return []
