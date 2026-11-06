"""智能路由引擎（AutoBuddy 第 7 个核心功能，对标 Magpie routing group）。

设计目标
--------
Magpie 的路由核心是 **routing group + smart failover**：
把多个账号 / 模型编成一组，请求按顺序尝试，遇到额度耗尽或报错就自动切到下一个，
调用方全程无感（``magpie group add ... routing=smart``）。

AutoBuddy 内部已有账号池轮询、能力矩阵换号、可用性巡检这套“黑盒自动换号”能力，
本模块把它**前台化 + 可编排化**：

1. **路由规则（routing_rules）**：按 ``api_key`` / ``model`` / ``client_tag`` 匹配请求，
   命中后分流到指定**路由组** / **指定账号** / **指定模型**。
2. **路由组（routing_groups）**：有序成员列表 + 模式（smart 顺序故障转移 / round_robin
   / order）+ 兜底策略（组内耗尽后退回全局账号池，或直接报错透传）。
3. **路由决策层**：在 ``select_account`` 选号之前插入，复用现有换号重试与能力矩阵，
   不重复实现故障转移。

本模块只负责“决策”（返回约束条件），真正的账号选择、并发平摊、换号重试、
能力矩阵学习仍由 ``main.select_account`` / ``chat_completions`` 承担 ——
这是与 Magpie 同构但复用 AutoBuddy 既有能力的关键设计。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from db import get_db_connection

# ---------------------------------------------------------------------------
# 常量与校验
# ---------------------------------------------------------------------------

VALID_MATCH_FIELDS = ("api_key", "model", "client_tag")
VALID_MATCH_OPS = ("eq", "contains")
VALID_TARGET_TYPES = ("group", "account", "model")
VALID_GROUP_MODES = ("smart", "round_robin", "order")
VALID_FALLBACKS = ("global_pool", "error")


def init_routing_db(conn: sqlite3.Connection) -> None:
    """在已打开的连接上创建路由相关表（幂等）。"""
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS routing_groups (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'smart',
        fallback TEXT NOT NULL DEFAULT 'global_pool',
        members TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER NOT NULL DEFAULT 1,
        priority INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS routing_rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        match_field TEXT NOT NULL DEFAULT 'model',
        match_op TEXT NOT NULL DEFAULT 'eq',
        match_value TEXT NOT NULL DEFAULT '',
        target_type TEXT NOT NULL DEFAULT 'group',
        target_id TEXT,
        target_model TEXT,
        priority INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""")
    conn.commit()


# ---------------------------------------------------------------------------
# 路由组 CRUD
# ---------------------------------------------------------------------------

def _row_to_group(r: Dict[str, Any]) -> Dict[str, Any]:
    try:
        members = json.loads(r.get("members") or "[]")
    except Exception:
        members = []
    return {
        "id": r["id"],
        "name": r["name"],
        "mode": r.get("mode") or "smart",
        "fallback": r.get("fallback") or "global_pool",
        "members": members if isinstance(members, list) else [],
        "enabled": bool(r.get("enabled", 1)),
        "priority": int(r.get("priority", 0) or 0),
    }


def _row_to_rule(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "name": r["name"],
        "match_field": r.get("match_field") or "model",
        "match_op": r.get("match_op") or "eq",
        "match_value": r.get("match_value") or "",
        "target_type": r.get("target_type") or "group",
        "target_id": r.get("target_id"),
        "target_model": r.get("target_model"),
        "enabled": bool(r.get("enabled", 1)),
        "priority": int(r.get("priority", 0) or 0),
    }


def list_groups() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        init_routing_db(conn)
        rows = conn.execute(
            "SELECT * FROM routing_groups ORDER BY priority ASC, name ASC"
        ).fetchall()
    return [_row_to_group(dict(r)) for r in rows]


def get_group(group_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        init_routing_db(conn)
        r = conn.execute(
            "SELECT * FROM routing_groups WHERE id = ?", (group_id,)
        ).fetchone()
    return _row_to_group(dict(r)) if r else None


def create_group(name: str, mode: str = "smart",
                 fallback: str = "global_pool",
                 members: Optional[List[Dict[str, Any]]] = None,
                 priority: int = 0) -> Dict[str, Any]:
    mode = mode if mode in VALID_GROUP_MODES else "smart"
    fallback = fallback if fallback in VALID_FALLBACKS else "global_pool"
    members = members or []
    gid = "grp_" + uuid.uuid4().hex[:12]
    now = time.time()
    with get_db_connection() as conn:
        init_routing_db(conn)
        conn.execute(
            """INSERT INTO routing_groups
               (id, name, mode, fallback, members, enabled, priority, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (gid, name, mode, fallback, json.dumps(members, ensure_ascii=False),
             priority, now, now),
        )
    return get_group(gid)


def update_group(group_id: str, **fields) -> Optional[Dict[str, Any]]:
    allowed = {"name", "mode", "fallback", "members", "enabled", "priority"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "mode" and v not in VALID_GROUP_MODES:
            v = "smart"
        if k == "fallback" and v not in VALID_FALLBACKS:
            v = "global_pool"
        if k == "members" and isinstance(v, list):
            v = json.dumps(v, ensure_ascii=False)
        if k == "enabled":
            v = 1 if v else 0
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return get_group(group_id)
    sets.append("updated_at = ?")
    vals.append(time.time())
    vals.append(group_id)
    with get_db_connection() as conn:
        init_routing_db(conn)
        conn.execute(
            "UPDATE routing_groups SET " + ", ".join(sets) + " WHERE id = ?",
            vals,
        )
    return get_group(group_id)


def delete_group(group_id: str) -> bool:
    with get_db_connection() as conn:
        init_routing_db(conn)
        cur = conn.execute("DELETE FROM routing_groups WHERE id = ?", (group_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 路由规则 CRUD
# ---------------------------------------------------------------------------

def list_rules() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        init_routing_db(conn)
        rows = conn.execute(
            "SELECT * FROM routing_rules ORDER BY priority ASC, name ASC"
        ).fetchall()
    return [_row_to_rule(dict(r)) for r in rows]


def get_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        init_routing_db(conn)
        r = conn.execute(
            "SELECT * FROM routing_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    return _row_to_rule(dict(r)) if r else None


def create_rule(name: str, match_field: str = "model", match_op: str = "eq",
                match_value: str = "", target_type: str = "group",
                target_id: Optional[str] = None,
                target_model: Optional[str] = None,
                priority: int = 0) -> Dict[str, Any]:
    match_field = match_field if match_field in VALID_MATCH_FIELDS else "model"
    match_op = match_op if match_op in VALID_MATCH_OPS else "eq"
    target_type = target_type if target_type in VALID_TARGET_TYPES else "group"
    rid = "rule_" + uuid.uuid4().hex[:12]
    now = time.time()
    with get_db_connection() as conn:
        init_routing_db(conn)
        conn.execute(
            """INSERT INTO routing_rules
               (id, name, match_field, match_op, match_value, target_type,
                target_id, target_model, priority, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (rid, name, match_field, match_op, match_value, target_type,
             target_id, target_model, priority, now, now),
        )
    return get_rule(rid)


def update_rule(rule_id: str, **fields) -> Optional[Dict[str, Any]]:
    allowed = {"name", "match_field", "match_op", "match_value",
               "target_type", "target_id", "target_model", "enabled", "priority"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "match_field" and v not in VALID_MATCH_FIELDS:
            v = "model"
        if k == "match_op" and v not in VALID_MATCH_OPS:
            v = "eq"
        if k == "target_type" and v not in VALID_TARGET_TYPES:
            v = "group"
        if k == "enabled":
            v = 1 if v else 0
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return get_rule(rule_id)
    sets.append("updated_at = ?")
    vals.append(time.time())
    vals.append(rule_id)
    with get_db_connection() as conn:
        init_routing_db(conn)
        conn.execute(
            "UPDATE routing_rules SET " + ", ".join(sets) + " WHERE id = ?",
            vals,
        )
    return get_rule(rule_id)


def delete_rule(rule_id: str) -> bool:
    with get_db_connection() as conn:
        init_routing_db(conn)
        cur = conn.execute("DELETE FROM routing_rules WHERE id = ?", (rule_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 路由决策层（核心）
# ---------------------------------------------------------------------------

def _rule_matches(rule: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    """判断一条规则是否命中当前请求。

    ctx 提供：model（原始请求模型名）、api_key（访问密钥 ID）、
    client_tag（客户端标识，来自 x-autobuddy-client 头）。
    """
    field = rule["match_field"]
    target = (ctx.get(field) or "").strip()
    value = (rule.get("match_value") or "").strip()
    if not value:
        return False
    op = rule["match_op"]
    if op == "contains":
        return value.lower() in target.lower()
    # 默认 eq（大小写不敏感，便于 model 名匹配）
    return target.lower() == value.lower()


def _acc_id(acc: Dict[str, Any]) -> Optional[str]:
    return acc.get("id") or acc.get("account_id")


def resolve_routing(raw_model: Optional[str],
                    routing_ctx: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
    """为一次请求解析路由决策。

    返回结构（与 select_account 的 routing 参数对接，只给“约束条件”）：

    {
      "matched": bool,
      "rule_id": str|None, "rule_name": str|None,
      "target_type": "group"|"account"|"model"|"none",
      "forced_account_id": str|None,   # target_type=account
      "forced_model": str|None,       # target_type=model（实际送出的模型名）
      "account_ids": list|None,       # target_type=group：候选账号白名单（按组顺序）
      "group_mode": str,
      "fallback": "global_pool"|"error",
      "group_id": str|None, "group_name": str|None,
    }

    未命中任何规则时返回 matched=False、各约束为 None，调用方照常走全局池。
    """
    ctx = routing_ctx or {}
    ctx_model = (raw_model or "").strip()
    ctx["model"] = ctx_model
    decision: Dict[str, Any] = {
        "matched": False,
        "rule_id": None, "rule_name": None,
        "target_type": "none",
        "forced_account_id": None,
        "forced_model": None,
        "account_ids": None,
        "group_mode": "smart",
        "fallback": "global_pool",
        "group_id": None, "group_name": None,
    }
    rules = list_rules()
    for rule in rules:
        if not rule["enabled"]:
            continue
        if not _rule_matches(rule, ctx):
            continue
        decision["matched"] = True
        decision["rule_id"] = rule["id"]
        decision["rule_name"] = rule["name"]
        ttype = rule["target_type"]
        decision["target_type"] = ttype
        if ttype == "account":
            decision["forced_account_id"] = rule.get("target_id")
            return decision
        if ttype == "model":
            decision["forced_model"] = rule.get("target_model") or rule.get("target_id")
            return decision
        if ttype == "group":
            gid = rule.get("target_id")
            grp = get_group(gid) if gid else None
            if not grp or not grp["enabled"]:
                # 组不存在/未启用：该规则视为不命中，继续尝试后续规则。
                decision["matched"] = False
                decision["rule_id"] = None
                decision["rule_name"] = None
                decision["target_type"] = "none"
                continue
            member_ids = [m.get("id") for m in grp["members"]
                          if isinstance(m, dict) and m.get("id")]
            decision["account_ids"] = member_ids
            decision["group_mode"] = grp["mode"]
            decision["fallback"] = grp["fallback"]
            decision["group_id"] = grp["id"]
            decision["group_name"] = grp["name"]
            return decision
    return decision


def apply_group_ordering(candidates: List[Dict[str, Any]],
                         allowed_ids: Optional[List[str]],
                         mode: str) -> List[Dict[str, Any]]:
    """把全局候选集收窄到路由组白名单，并按组模式排序。

    - order / smart：严格按 allowed_ids 出现顺序（故障转移由 select_account
      的换号重试天然承担，这里只保证顺序）。
    - round_robin：组内按顺序轮转（基于进程内轮转计数）。
    返回空列表表示组内无可用成员（调用方按 fallback 决定下一步）。
    """
    if not allowed_ids:
        return candidates
    aid_set = {str(i) for i in allowed_ids}
    kept = [a for a in candidates if str(_acc_id(a)) in aid_set]
    if not kept:
        return kept
    if mode == "round_robin":
        return _round_robin_order(allowed_ids, kept)
    pos = {str(i): idx for idx, i in enumerate(allowed_ids)}
    kept.sort(key=lambda a: pos.get(str(_acc_id(a)), 9999))
    return kept


_ROUND_ROBIN: Dict[str, int] = {}


def _round_robin_order(allowed_ids: List[str],
                        kept: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    key = "|".join(allowed_ids)
    start = _ROUND_ROBIN.get(key, 0) % max(len(allowed_ids), 1)
    ordered_ids = allowed_ids[start:] + allowed_ids[:start]
    _ROUND_ROBIN[key] = (start + 1) % max(len(allowed_ids), 1)
    pos = {str(i): idx for idx, i in enumerate(ordered_ids)}
    kept.sort(key=lambda a: pos.get(str(_acc_id(a)), 9999))
    return kept
