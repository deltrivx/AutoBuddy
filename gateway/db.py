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
from typing import Optional, Dict, Any

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
        
        # 检查是否已存在管理员
        cursor.execute("SELECT COUNT(*) as cnt FROM users")
        row = cursor.fetchone()
        if row and row["cnt"] == 0:
            default_user = os.getenv("AUTH_DEFAULT_USER", "admin")
            default_pwd = os.getenv("AUTH_DEFAULT_PASS", "admin123")
            salt = secrets.token_hex(16)
            pwd_hash = hashlib.sha256((default_pwd + salt).encode("utf-8")).hexdigest()
            now = time.time()
            cursor.execute(
                "INSERT INTO users (username, password_hash, salt, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (default_user, pwd_hash, salt, "admin", now, now)
            )
            print(f"[Database] 已初始化默认管理员账号：{default_user} (初始密码: admin123)")

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
