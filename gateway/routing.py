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

# 匹配字段：基础字段（与请求头/模型直接对应）。
VALID_MATCH_FIELDS = ("api_key", "model", "client_tag", "variant")
VALID_MATCH_OPS = ("eq", "contains")
VALID_TARGET_TYPES = ("group", "account", "model")
# 路由组分发模式 —— 对齐 Magpie 的 ROUTE_OPTS / GROUP_ROUTE_OPTS：
#   smart        智能：按健康/负载/剩余额度，故障自动转移（AutoBuddy 默认）
#   order        顺序：列表顺序，第一个不行再下一个（In order）
#   drain        用尽为止：逐个账号用到耗尽再换下一个（Until used up）
#   rotate       轮转：每次请求顺时针走下一个成员（In turn）
#   usage        最少使用优先：剩余额度最多的账号先上（Least used）
#   pace         到期前先用：剩余/小时 最大者先上（Use before expiry）
#   free         免费优先：免费/不耗额度的账号先上（Free first）
#   fast         速度优先：最近首 token 最快者先上（Speed first）
#   weight       按权重：成员带权重，按权重加权轮转（By weight）
#   manual       手动：固定用 pick 指定的成员（Manual）
VALID_GROUP_MODES = ("smart", "order", "drain", "rotate", "usage", "pace",
                     "free", "fast", "weight", "manual")
VALID_FALLBACKS = ("global_pool", "error")
# 会话粘性（对标 Magpie Affinity：Auto/Session/Within turn/Off/Strict）
VALID_AFFINITY = ("none", "session", "turn", "strict")
# 下沉（对标 Magpie Sink：Keeps its place / Goes to the back）
VALID_SINK = (0, 1)


# 组模式中文标签（前端展示用，对齐 Magpie i18n）
GROUP_MODE_LABELS = {
    "smart": "智能",
    "order": "顺序",
    "drain": "用尽为止",
    "rotate": "轮转",
    "usage": "最少使用优先",
    "pace": "到期前先用",
    "free": "免费优先",
    "fast": "速度优先",
    "weight": "按权重",
    "manual": "手动",
}
AFFINITY_LABELS = {
    "none": "关闭",
    "session": "会话",
    "turn": "回合内",
    "strict": "严格",
}


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
    # 全局路由配置（对标 Magpie 的 Routing / Stays / Rate-limited / Fallback）：
    #   · mode:        请求在账号池里的分发方式（smart/order/drain/rotate/usage/pace/free/fast/weight/manual）。默认 smart。
    #   · affinity:    粘性（none 关闭 / session 会话 / turn 回合内 / strict 严格），对标 Magpie Stays。
    #   · sink:        被限流/降速的账号自动排到其余账号之后（对标 Magpie Rate-limited）。
    #   · fallback:    有序的兜底链（组 id 或 "global_pool"），账号没额度/挂了按顺序切。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS routing_config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    # 实时路由轨迹（对标 Magpie 的 /api/gateway/trace：每个请求谁被排第一、为什么、
    # 每次换号试了谁、最后谁答）。这是 Magpie 路由页最招牌的“实时演剧”功能。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS routing_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        req_id TEXT NOT NULL,
        ts REAL NOT NULL,
        model TEXT,
        client_tag TEXT,
        api_key TEXT,
        matched_rule TEXT,
        group_id TEXT,
        group_name TEXT,
        first_account TEXT,
        final_account TEXT,
        why TEXT,
        tries TEXT NOT NULL DEFAULT '[]',
        "order" TEXT NOT NULL DEFAULT '[]',
        swapped INTEGER NOT NULL DEFAULT 0,
        routed INTEGER NOT NULL DEFAULT 0,
        tokens INTEGER,
        ms INTEGER,
        done INTEGER NOT NULL DEFAULT 0,
        status TEXT,
        error TEXT
    )""")
    # 增量升级已有表结构（新增列），保证旧数据不丢。
    _migrate_groups(cur)
    _migrate_rules(cur)
    _migrate_traces(cur)
    conn.commit()


def _migrate_traces(cur) -> None:
    existing = {r[1] for r in cur.execute("PRAGMA table_info(routing_traces)").fetchall()}
    if "order" not in existing:
        cur.execute("ALTER TABLE routing_traces ADD COLUMN \"order\" TEXT NOT NULL DEFAULT '[]'")


def _migrate_groups(cur) -> None:
    existing = {r[1] for r in cur.execute("PRAGMA table_info(routing_groups)").fetchall()}
    for col, ddl in (
        ("affinity", "ALTER TABLE routing_groups ADD COLUMN affinity TEXT DEFAULT 'session'"),
        ("sink", "ALTER TABLE routing_groups ADD COLUMN sink INTEGER DEFAULT 1"),
        ("first_token", "ALTER TABLE routing_groups ADD COLUMN first_token INTEGER DEFAULT 0"),
        ("patience", "ALTER TABLE routing_groups ADD COLUMN patience INTEGER DEFAULT 0"),
        ("effort", "ALTER TABLE routing_groups ADD COLUMN effort TEXT DEFAULT ''"),
        ("levels", "ALTER TABLE routing_groups ADD COLUMN levels TEXT DEFAULT '[]'"),
        ("unlists", "ALTER TABLE routing_groups ADD COLUMN unlists INTEGER DEFAULT 0"),
        ("weights", "ALTER TABLE routing_groups ADD COLUMN weights TEXT DEFAULT '{}'"),
        ("pick", "ALTER TABLE routing_groups ADD COLUMN pick TEXT DEFAULT ''"),
    ):
        if col not in existing:
            try:
                cur.execute(ddl)
            except Exception:
                pass


def _migrate_rules(cur) -> None:
    existing = {r[1] for r in cur.execute("PRAGMA table_info(routing_rules)").fetchall()}
    if "match" not in existing:
        try:
            cur.execute("ALTER TABLE routing_rules ADD COLUMN match TEXT DEFAULT '{}'")
        except Exception:
            pass


# 全局路由配置默认值（与 Magpie 默认行为对齐）。
_DEFAULT_CONFIG = {
    "mode": "smart",        # smart | order | drain | rotate | usage | pace | free | fast | weight | manual
    "affinity": "session",  # none | session | turn | strict
    "sink": True,           # 限流账号后排（Rate-limited）
    "fallback": ["global_pool"],  # 兜底层：先退回全局账号池
}
_VALID_MODES = ("smart", "order", "drain", "rotate", "usage", "pace",
                "free", "fast", "weight", "manual")
_VALID_AFFINITY = ("none", "session", "turn", "strict")


def get_routing_config() -> Dict[str, Any]:
    """读取全局路由配置，缺字段补默认值。"""
    cfg: Dict[str, Any] = dict(_DEFAULT_CONFIG)
    try:
        with get_db_connection() as conn:
            init_routing_db(conn)
            rows = conn.execute("SELECT key, value FROM routing_config").fetchall()
        for r in rows:
            k = r["key"]
            try:
                v = json.loads(r["value"])
            except (ValueError, TypeError):
                v = r["value"]
            cfg[k] = v
    except Exception:
        pass
    # 字段合规化
    if cfg.get("mode") not in _VALID_MODES:
        cfg["mode"] = "smart"
    if cfg.get("affinity") not in _VALID_AFFINITY:
        cfg["affinity"] = "session"
    if not isinstance(cfg.get("sink"), bool):
        cfg["sink"] = True
    if not isinstance(cfg.get("fallback"), list) or not cfg["fallback"]:
        cfg["fallback"] = ["global_pool"]
    return cfg


def set_routing_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """更新全局路由配置（只接受白名单字段）。"""
    allowed = {"mode", "affinity", "sink", "fallback"}
    cfg = get_routing_config()
    for k, v in updates.items():
        if k not in allowed:
            continue
        if k == "mode" and v not in _VALID_MODES:
            v = "smart"
        if k == "affinity" and v not in _VALID_AFFINITY:
            v = "session"
        if k == "sink":
            v = bool(v)
        if k == "fallback" and isinstance(v, list):
            v = [str(x) for x in v if x]
            if not v:
                v = ["global_pool"]
        cfg[k] = v
    with get_db_connection() as conn:
        init_routing_db(conn)
        for k, v in cfg.items():
            conn.execute(
                "INSERT INTO routing_config(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v, ensure_ascii=False)),
            )
    return cfg


# ---------------------------------------------------------------------------
# 路由组 CRUD
# ---------------------------------------------------------------------------

def _row_to_group(r: Dict[str, Any]) -> Dict[str, Any]:
    try:
        members = json.loads(r.get("members") or "[]")
    except Exception:
        members = []
    try:
        weights = json.loads(r.get("weights") or "{}")
    except Exception:
        weights = {}
    try:
        levels = json.loads(r.get("levels") or "[]")
    except Exception:
        levels = []
    return {
        "id": r["id"],
        "name": r["name"],
        "mode": r.get("mode") or "smart",
        "fallback": r.get("fallback") or "global_pool",
        "members": members if isinstance(members, list) else [],
        "enabled": bool(r.get("enabled", 1)),
        "priority": int(r.get("priority", 0) or 0),
        # Magpie 组级路由属性
        "affinity": r.get("affinity") or "session",
        "sink": bool(int(r.get("sink", 1) or 1)),
        "first_token": int(r.get("first_token", 0) or 0),
        "patience": int(r.get("patience", 0) or 0),
        "effort": r.get("effort") or "",
        "levels": levels if isinstance(levels, list) else [],
        "unlists": bool(int(r.get("unlists", 0) or 0)),
        "weights": weights if isinstance(weights, dict) else {},
        "pick": r.get("pick") or "",
    }


def _row_to_rule(r: Dict[str, Any]) -> Dict[str, Any]:
    try:
        match = json.loads(r.get("match") or "{}")
    except Exception:
        match = {}
    if not isinstance(match, dict):
        match = {}
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
        # Magpie 富规则条件（tokens≥N / 有图 / reasoning / 指定 agent / intent /
        # compact / time / folders）—— 与基础 match_field 取并集，全部满足才命中。
        "match": match,
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
                 priority: int = 0,
                 affinity: str = "session", sink: bool = True,
                 first_token: int = 0, patience: int = 0,
                 effort: str = "", levels: Optional[List[str]] = None,
                 unlists: bool = False, weights: Optional[Dict[str, int]] = None,
                 pick: str = "") -> Dict[str, Any]:
    mode = mode if mode in VALID_GROUP_MODES else "smart"
    fallback = fallback if fallback in VALID_FALLBACKS else "global_pool"
    affinity = affinity if affinity in VALID_AFFINITY else "session"
    members = members or []
    weights = weights or {}
    levels = levels or []
    gid = "grp_" + uuid.uuid4().hex[:12]
    now = time.time()
    with get_db_connection() as conn:
        init_routing_db(conn)
        conn.execute(
            """INSERT INTO routing_groups
               (id, name, mode, fallback, members, enabled, priority, created_at, updated_at,
                affinity, sink, first_token, patience, effort, levels, unlists, weights, pick)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (gid, name, mode, fallback, json.dumps(members, ensure_ascii=False),
             priority, now, now,
             affinity, 1 if sink else 0, int(first_token or 0), int(patience or 0),
             effort or "", json.dumps(levels, ensure_ascii=False),
             1 if unlists else 0, json.dumps(weights, ensure_ascii=False), pick or ""),
        )
    return get_group(gid)


def update_group(group_id: str, **fields) -> Optional[Dict[str, Any]]:
    allowed = {"name", "mode", "fallback", "members", "enabled", "priority",
               "affinity", "sink", "first_token", "patience", "effort",
               "levels", "unlists", "weights", "pick"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "mode" and v not in VALID_GROUP_MODES:
            v = "smart"
        if k == "fallback" and v not in VALID_FALLBACKS:
            v = "global_pool"
        if k == "affinity" and v not in VALID_AFFINITY:
            v = "session"
        if k == "members" and isinstance(v, list):
            v = json.dumps(v, ensure_ascii=False)
        if k == "weights" and isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
        if k == "levels" and isinstance(v, list):
            v = json.dumps(v, ensure_ascii=False)
        if k in ("sink", "unlists", "enabled"):
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
                priority: int = 0,
                match: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
                target_id, target_model, priority, enabled, created_at, updated_at, match)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (rid, name, match_field, match_op, match_value, target_type,
             target_id, target_model, priority, now, now,
             json.dumps(match or {}, ensure_ascii=False)),
        )
    return get_rule(rid)


def update_rule(rule_id: str, **fields) -> Optional[Dict[str, Any]]:
    allowed = {"name", "match_field", "match_op", "match_value",
               "target_type", "target_id", "target_model", "enabled", "priority",
               "match"}
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
        if k == "match" and isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
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

    基础匹配（match_field/match_op/match_value）与 Magpie 富规则（match 块）取
    并集，全部满足才算命中：
      · match_field/match_op/match_value：model / api_key / client_tag 的 eq/contains
      · match.tokens：请求估算 token 数 ≥ N
      · match.has_image：请求含图片
      · match.effort：reasoning 开关（on）或等级（≥level）
      · match.agents：命中请求的 agent/client 标识之一
      · match.intent：请求意图关键词
      · match.compact：是否正在压缩上下文
      · match.time / match.folders：时间/文件夹（需请求上下文提供，缺席时忽略）
    """
    # 1) 基础字段匹配
    field = rule["match_field"]
    if field == "variant":
        # variant 是请求模型实际归属的上游变体（cn / ai），由网关按模型目录解析后填入
        # routing_ctx；这是真正能区分国内/国际版账号的信号，不再用 client_tag 的
        # 子串匹配（旧逻辑 "ai" 会误命中 "OpenAI" 的 user-agent，导致每条请求都错判）。
        target = (ctx.get("variant") or "").strip().lower()
        value = (rule.get("match_value") or "").strip().lower()
        if value:
            op = rule["match_op"]
            if op == "contains":
                if value not in target:
                    return False
            else:
                if target != value:
                    return False
    else:
        target = (ctx.get(field) or "").strip()
        value = (rule.get("match_value") or "").strip()
        if value:
            op = rule["match_op"]
            if op == "contains":
                if value.lower() not in target.lower():
                    return False
            else:
                if target.lower() != value.lower():
                    return False

    # 2) 富规则条件（match 块）
    match = rule.get("match") or {}
    if not isinstance(match, dict):
        return True if value else False
    # 空 match_value 且无任何富条件 → 规则不完整，永不命中（守卫）
    if not value and not any(k in match for k in (
        "tokens", "images", "effort", "agents", "intent", "compact", "time", "folders")):
        return False

    # tokens ≥ N
    if match.get("tokens"):
        try:
            if int(ctx.get("tokens") or 0) < int(match["tokens"]):
                return False
        except (TypeError, ValueError):
            pass
    # 含图片
    if match.get("images") and not ctx.get("has_image"):
        return False
    # reasoning 开关 / 等级
    eff = match.get("effort")
    if eff:
        req_eff = (ctx.get("effort") or "").lower()
        if eff == "on":
            if not req_eff:
                return False
        else:
            # 形如 "high" / ">=medium"：要求请求等级不低于指定等级
            order = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
            need = str(eff).replace(">=", "").strip().lower()
            if need in order and req_eff in order:
                if order.index(req_eff) < order.index(need):
                    return False
    # 指定 agent / client
    agents = match.get("agents") or []
    if agents:
        req_agent = (ctx.get("agent") or ctx.get("client_tag") or "").lower()
        if not any(str(a).lower() in req_agent or req_agent in str(a).lower() for a in agents):
            return False
    # 意图关键词
    intent = (match.get("intent") or "").strip()
    if intent:
        prompt = (ctx.get("prompt") or "").lower()
        if intent.lower() not in prompt:
            return False
    # 正在压缩上下文
    if match.get("compact") and not ctx.get("compact"):
        return False
    # time / folders：需请求上下文提供对应字段，缺失则跳过（不阻断命中）
    if match.get("time") and ctx.get("time") is not None:
        # 简单实现：match.time 为 {"hour": [start,end]} 的小时区间
        try:
            rng = match["time"].get("hour")
            if rng and not (int(rng[0]) <= int(ctx["time"]) <= int(rng[1])):
                return False
        except Exception:
            pass
    if match.get("folders"):
        req_folders = ctx.get("folders") or []
        if not any(f in req_folders for f in match["folders"]):
            return False
    return True


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
        "why": "",
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
            decision["why"] = f"规则「{rule['name']}」指定账号 {rule.get('target_id')}"
            return decision
        if ttype == "model":
            decision["forced_model"] = rule.get("target_model") or rule.get("target_id")
            decision["why"] = f"规则「{rule['name']}」改写模型为 {decision['forced_model']}"
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
            decision["why"] = (f"规则「{rule['name']}」命中路由组「{grp['name']}」"
                               f"（模式 {GROUP_MODE_LABELS.get(grp['mode'], grp['mode'])}」")
            return decision
    return decision


def _account_stats() -> Dict[str, Dict[str, Any]]:
    """聚合每个账号的路由用统计（对齐 Magpie 的 per-key 状态）：
    total —— 累计 token（最少使用优先用）；
    avg_ms —— 平均首响/耗时（速度优先用）；
    cost —— 累计花费（免费优先用，cost==0 优先）；
    picks —— 被选次数（轮转/最少使用兜底用）。
    只读不写，异常时返回空，调用方退化到顺序。
    """
    stats: Dict[str, Dict[str, Any]] = {}
    try:
        import os as _os
        db_path = _os.path.join(str(DATA_DIR), "token_stats.db")
        if _os.path.exists(db_path):
            import sqlite3 as _sql
            c = _sql.connect(db_path)
            for aid, tot, dur, cost in c.execute(
                "SELECT account_id, COALESCE(SUM(input+output),0), "
                "COALESCE(AVG(duration),0), COALESCE(SUM(cost),0) FROM usage_logs GROUP BY account_id"
            ).fetchall():
                stats[aid] = {"total": tot or 0, "avg_ms": dur or 0, "cost": cost or 0}
            c.close()
    except Exception:
        pass
    # 被选次数（selection_logs.json），补足没有调用记录的账号
    try:
        import json as _json, os as _os
        sl = _os.path.join(str(DATA_DIR), "selection_logs.json")
        if _os.path.exists(sl):
            arr = _json.load(open(sl))
            if isinstance(arr, list):
                for r in arr:
                    aid = r.get("accountId")
                    if aid:
                        stats.setdefault(aid, {"total": 0, "avg_ms": 0, "cost": 0})
                        stats[aid]["picks"] = stats[aid].get("picks", 0) + 1
    except Exception:
        pass
    return stats


def apply_group_ordering(candidates: List[Dict[str, Any]],
                         allowed_ids: Optional[List[str]],
                         mode: str,
                         weights: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
    """把全局候选集收窄到路由组白名单，并按组模式排序/分发（1:1 对齐 Magpie 账号间路由）。

    模式语义（与 Magpie 一致）：
      · smart / order —— 严格按 allowed_ids 顺序（故障转移由 select_account 换号重试承担）；
      · rotate（轮流）—— 进程内轮转计数，每个请求取下一个；
      · drain（用尽为止）—— 用当前账号直到出错/耗尽再换下一个（即顺序 + 粘当前）；
      · usage（最少使用优先）—— 累计 token 最少者先上；
      · pace（到期前先用）—— 无到期数据时退化为「最少被选/最近最闲」；
      · free（免费优先）—— cost==0 的账号排前面；
      · fast（速度优先）—— 平均首响最快者先上；
      · weight（按权重）—— 依组 weights 做加权随机；
      · manual —— 调用方已指定，原样返回。
    返回空列表表示组内无可用成员（调用方按 fallback 决定下一步）。
    """
    if not allowed_ids:
        return candidates
    aid_set = {str(i) for i in allowed_ids}
    kept = [a for a in candidates if str(_acc_id(a)) in aid_set]
    if not kept:
        return kept
    mode = (mode or "smart").lower()
    if mode == "rotate":
        return _round_robin_order(allowed_ids, kept)
    if mode in ("smart", "order", "drain", "manual"):
        # 顺序：故障转移由 select_account 的换号重试天然承担
        pos = {str(i): idx for idx, i in enumerate(allowed_ids)}
        kept.sort(key=lambda a: pos.get(str(_acc_id(a)), 9999))
        return kept
    st = _account_stats()
    def _aid(a): return str(_acc_id(a))
    if mode == "usage":
        # 最少使用优先：累计 token 升序；无记录者（total==0）排最前
        kept.sort(key=lambda a: st.get(_aid(a), {}).get("total", 0))
        return kept
    if mode == "free":
        # 免费优先：cost==0 在前，其余按 cost 升序
        kept.sort(key=lambda a: st.get(_aid(a), {}).get("cost", 0))
        return kept
    if mode == "fast":
        # 速度优先：平均耗时升序；无记录者排最后
        kept.sort(key=lambda a: (st.get(_aid(a), {}).get("avg_ms", 0) or 1e9,))
        return kept
    if mode == "pace":
        # 到期前先用：无到期数据时退化为「最少被选」优先，让久未上场的先上
        kept.sort(key=lambda a: st.get(_aid(a), {}).get("picks", 0))
        return kept
    if mode == "weight":
        # 按权重：组内账号按 weights 加权随机抽一个排头，其余保持顺序
        global _WEIGHTS_CACHE
        _WEIGHTS_CACHE = {str(k): float(v) for k, v in (weights or {}).items() if v is not None}
        return _weighted_order(allowed_ids, kept)
    # 兜底：顺序
    pos = {str(i): idx for idx, i in enumerate(allowed_ids)}
    kept.sort(key=lambda a: pos.get(str(_acc_id(a)), 9999))
    return kept


_ROUND_ROBIN: Dict[str, int] = {}


def _weighted_order(allowed_ids: List[str], kept: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按组 weights 做加权随机：抽中的账号排到最前，其余保持白名单顺序。
    权重取不到时按 1 计（等权）。
    """
    try:
        import random as _rnd
        weights = {str(k): float(v) for k, v in (_WEIGHTS_CACHE or {}).items()}
        ids = [str(i) for i in allowed_ids]
        w = [max(weights.get(i, 1.0), 0.0) for i in ids]
        if sum(w) <= 0:
            w = [1.0] * len(ids)
        chosen = _rnd.choices(ids, weights=w, k=1)[0]
        ordered = [chosen] + [i for i in ids if i != chosen]
        pos = {i: idx for idx, i in enumerate(ordered)}
        kept.sort(key=lambda a: pos.get(str(_acc_id(a)), 9999))
        return kept
    except Exception:
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


_WEIGHTS_CACHE: Dict[str, float] = {}


# ---------------------------------------------------------------------------
# 自动生成（对标 Magpie：路由组/规则不应让用户手工逐个配，应从账号池自动推导）
# ---------------------------------------------------------------------------

def auto_generate(accounts: List[Dict[str, Any]], pool_config: Dict[str, Any]) -> Dict[str, Any]:
    """从账号池自动推导出一套可用默认：一个“全账号池组” + 分档规则。

    与 Magpie 的自动 routing group 同理 —— 用户不必手工逐个加组/规则，
    打开智能路由页点「自动生成」即可获得一组开箱即用的默认编排：
      · 组：把所有可用账号编成一组（smart 顺序故障转移，组内耗尽退回全局池）；
      · 规则：按 callRouting 档位（cn / ai / all）各生成一条，命中后分流到该组，
        让“按区域/档位分流”零配置可用。
    已有内容时不清空、不覆盖（只新增），避免误删用户自定义。
    """
    # 与选号同口径：先过白名单 + 可用性
    enabled = {str(x) for x in (pool_config.get("enabledAccountIds") or [])}
    candidates = accounts if not enabled else [
        a for a in accounts if str(_acc_id(a)) in enabled]
    disabled = set()
    try:
        from account_policy import disabled_ids as _pol_disabled
        disabled = _pol_disabled()
    except Exception:
        disabled = set()
    members = [
        {"id": str(_acc_id(a)), "type": "account"}
        for a in candidates
        if _acc_id(a) and str(_acc_id(a)) not in disabled
    ]
    if not members:
        return {"ok": False, "reason": "account_pool_empty",
                "detail": "账号池当前没有可用账号，无法生成路由组"}

    # 去重：若已存在同名“全账号池组”则复用，不再重复创建
    existing = list_groups()
    grp = None
    for g in existing:
        if g["name"] == "全账号池组":
            grp = g
            break
    if not grp:
        grp = create_group(
            name="全账号池组", mode="smart", fallback="global_pool",
            members=members, priority=0)
    else:
        # 合并成员（不丢已有），更新为最新可用集合
        cur = {m.get("id") for m in (grp.get("members") or [])}
        merged = list(grp.get("members") or [])
        for m in members:
            if m["id"] not in cur:
                merged.append(m)
        update_group(grp["id"], members=merged)
        grp = get_group(grp["id"])

    # 清理历史遗留的坏规则：旧 auto_generate 用 client_tag 子串匹配
    # （"ai" 会误命中 "OpenAI" 的 user-agent），导致每条请求都错判为“国际版”。
    # 重新生成时先把这类 client_tag 匹配的规则删掉，再建正确的 variant 规则。
    for old in list_rules():
        if old.get("match_field") == "client_tag":
            try:
                delete_rule(old["id"])
            except Exception:
                pass

    routing_mode = str(pool_config.get("callRouting") or "all").strip().lower()
    rule_specs = []
    if routing_mode == "cn":
        rule_specs.append(("国内版请求分流", "variant", "eq", "cn", grp["id"]))
    elif routing_mode == "ai":
        rule_specs.append(("国际版请求分流", "variant", "eq", "ai", grp["id"]))
    else:
        # all：按模型实际归属的上游变体拆两条，覆盖国内/国际分流。
        # 用 variant（cn/ai）而非 client_tag——client_tag 是 user-agent 字符串，
        # 旧逻辑 ai 会误命中 OpenAI，导致每条请求都错判为国际版。
        rule_specs.append(("国内版账号分流", "variant", "eq", "cn", grp["id"]))
        rule_specs.append(("国际版账号分流", "variant", "eq", "ai", grp["id"]))

    created_rules = []
    existing_rules = list_rules()
    for name, field, op, value, gid in rule_specs:
        if any(r["name"] == name for r in existing_rules):
            continue
        created_rules.append(create_rule(
            name=name, match_field=field, match_op=op, match_value=value,
            target_type="group", target_id=gid, priority=0))

    return {
        "ok": True,
        "group": grp,
        "created_rules": created_rules,
        "skipped_rules": len(rule_specs) - len(created_rules),
        "member_count": len(members),
    }


# ---------------------------------------------------------------------------
# 实时路由轨迹（对标 Magpie 的 /api/gateway/trace “实时演剧”）
# ---------------------------------------------------------------------------
# 进程内环形缓冲，避免高频写库；同时落库供历史查看。
_TRACE_RING: List[Dict[str, Any]] = []
_TRACE_SEQ = 0
_TRACE_LOCK = None

try:
    import threading
    _TRACE_LOCK = threading.Lock()
except Exception:
    _TRACE_LOCK = None


def _trace_alloc() -> int:
    global _TRACE_SEQ
    if _TRACE_LOCK:
        with _TRACE_LOCK:
            _TRACE_SEQ += 1
            return _TRACE_SEQ
    _TRACE_SEQ += 1
    return _TRACE_SEQ


def trace_begin(req_id: str, ctx: Dict[str, Any],
                decision: Dict[str, Any]) -> int:
    """一次请求进入选号时调用，登记轨迹首条记录，返回 trace id。

    对齐 Magpie 实时路由轨迹（/api/gateway/trace）：每个请求把“谁被排第一、
    为什么、每次换号试了谁、最后谁答”都记下来，实时演出来。
    """
    tid = _trace_alloc()
    rec = {
        "id": tid,
        "req_id": req_id,
        "ts": time.time(),
        "model": ctx.get("model") or "",
        "client_tag": ctx.get("client_tag") or "",
        "api_key": ctx.get("api_key") or "",
        "key_name": ctx.get("key_name") or "",
        # Magpie 等价字段：这次请求从哪个 caller（客户端/应用）进来
        "caller_key": ctx.get("caller_key") or "",
        "caller_name": ctx.get("caller_name") or "",
        # 实际送出用的上游供应商（如 codebuddy/cn/ai）
        "provider": ctx.get("provider") or "",
        # 候选名单（按路由决策排出的尝试顺序）；每条含 name/who/plan/kind
        "order": ctx.get("order") or [],
        "matched_rule": decision.get("rule_name") or ("" if decision.get("matched") else ""),
        "group_id": decision.get("group_id"),
        "group_name": decision.get("group_name"),
        "first_account": None,
        "final_account": None,
        "why": decision.get("why") or "",
        "tries": [],
        "swapped": 0,
        "routed": 1 if decision.get("matched") else 0,
        "rerouted": 0,
        "tokens": None,
        "ms": None,
        "bytes": None,
        "content_at": None,
        "done": 0,
        "status": None,
        "error": None,
    }
    _push_ring(rec)
    return tid


def _push_ring(rec: Dict[str, Any]) -> None:
    # 落库（轻量）
    try:
        with get_db_connection() as conn:
            init_routing_db(conn)
            conn.execute(
                """INSERT INTO routing_traces
                   (req_id, ts, model, client_tag, api_key, matched_rule, group_id,
                    group_name, first_account, final_account, why, tries, "order", swapped,
                    routed, tokens, ms, done, status, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec["req_id"], rec["ts"], rec["model"], rec["client_tag"],
                 rec["api_key"], rec["matched_rule"], rec["group_id"],
                 rec["group_name"], rec["first_account"], rec["final_account"],
                 rec["why"], json.dumps(rec["tries"], ensure_ascii=False),
                 json.dumps(rec.get("order") or [], ensure_ascii=False),
                 rec["swapped"], rec["routed"], rec["tokens"], rec["ms"],
                 rec["done"], rec["status"], rec["error"]),
            )
    except Exception:
        pass
    if _TRACE_LOCK:
        with _TRACE_LOCK:
            _TRACE_RING.append(rec)
            if len(_TRACE_RING) > 200:
                _TRACE_RING.pop(0)
    else:
        _TRACE_RING.append(rec)
        if len(_TRACE_RING) > 200:
            _TRACE_RING.pop(0)


def _ring_find(tid: int) -> Optional[Dict[str, Any]]:
    if _TRACE_LOCK:
        with _TRACE_LOCK:
            for r in reversed(_TRACE_RING):
                if r["id"] == tid:
                    return r
    else:
        for r in reversed(_TRACE_RING):
            if r["id"] == tid:
                return r
    return None


def trace_try(tid: int, account_id: str, label: str = "",
               status: Optional[str] = None, ms: Optional[int] = None,
               error: Optional[str] = None, sent_by: str = "gateway") -> None:
    """记录一次候选账号尝试（对齐 Magpie 的 tries：每次试了谁、结果如何）。"""
    r = _ring_find(tid)
    if not r:
        return
    if r["first_account"] is None:
        r["first_account"] = account_id
    now = time.time()
    if not any(t.get("account") == account_id for t in r["tries"]):
        r["tries"].append({"account": account_id, "label": label,
                           "start": now, "done": status is not None,
                           "status": status, "ms": ms, "error": error,
                           "sent_by": sent_by})
    _push_ring(r)


def trace_reroute(tid: int, account_id: str, label: str = "fallback",
                  status: Optional[str] = None, ms: Optional[int] = None,
                  error: Optional[str] = None) -> None:
    """记录一次换号重试（Magpie 的 rerouted 计数 + 第二条 try）。"""
    r = _ring_find(tid)
    if not r:
        return
    r["swapped"] = 1
    r["rerouted"] = (r.get("rerouted") or 0) + 1
    now = time.time()
    r["tries"].append({"account": account_id, "label": label,
                       "start": now, "done": status is not None,
                       "status": status, "ms": ms, "error": error,
                       "sent_by": "gateway"})
    _push_ring(r)


def trace_set_final(tid: int, account_id: str, swapped: bool = False) -> None:
    r = _ring_find(tid)
    if not r:
        return
    r["final_account"] = account_id
    if swapped:
        r["swapped"] = 1
    _push_ring(r)


def trace_bytes(tid: int, nbytes: int) -> None:
    """Magpie 的 bytes/contentAt：记录本次响应体量。"""
    r = _ring_find(tid)
    if not r:
        return
    r["bytes"] = nbytes
    r["content_at"] = time.time()
    _push_ring(r)


def trace_finish(tid: int, ok: bool, status: Optional[str] = None,
                 error: Optional[str] = None, tokens: Optional[int] = None,
                 ms: Optional[int] = None) -> None:
    r = _ring_find(tid)
    if not r:
        return
    r["done"] = 1
    r["status"] = status
    r["error"] = error
    r["tokens"] = tokens
    r["ms"] = ms
    _push_ring(r)


def list_traces(after: int = 0, limit: int = 60) -> Dict[str, Any]:
    """返回轨迹列表（对标 Magpie trace 的 after=seq 增量协议）。

    优先读进程内环形缓冲（实时），不足时回退数据库历史。
    """
    if _TRACE_LOCK:
        with _TRACE_LOCK:
            ring = list(_TRACE_RING)
    else:
        ring = list(_TRACE_RING)
    # 始终返回**时间正序**（最旧→最新），与 Magpie trace 协议一致：
    # 前端取 traces[traces.length-1] 作为“当前正在演”的最新一条。
    ring = sorted(ring, key=lambda r: r.get("id") or 0)
    seq = max([r["id"] for r in ring], default=0)
    out = [r for r in ring if r["id"] > after][-limit:]
    if not out:
        # 回退数据库（DB 按 id DESC 取，需翻转成时间正序）
        try:
            with get_db_connection() as conn:
                init_routing_db(conn)
                rows = conn.execute(
                    "SELECT * FROM routing_traces ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            rows = list(reversed(rows))
            for r in rows:
                d = dict(r)
                try:
                    d["tries"] = json.loads(d.get("tries") or "[]")
                except Exception:
                    d["tries"] = []
                try:
                    d["order"] = json.loads(d.get("order") or "[]")
                except Exception:
                    d["order"] = []
                out.append({
                    "id": d["id"], "req_id": d["req_id"], "ts": d["ts"],
                    "model": d["model"], "client_tag": d["client_tag"],
                    "matched_rule": d["matched_rule"], "group_name": d["group_name"],
                    "first_account": d["first_account"],
                    "final_account": d["final_account"], "why": d["why"], "order": d.get("order") or [],
                    "tries": d["tries"], "swapped": d["swapped"],
                    "routed": d["routed"], "tokens": d["tokens"],
                    "ms": d["ms"], "done": d["done"], "status": d["status"],
                    "error": d["error"],
                })
            out = out[-limit:]
            seq = max([r["id"] for r in out], default=0)
        except Exception:
            pass
    # 统计（对齐 Magpie totals：请求/改道/错误）
    requests = len(out)
    rerouted = sum(1 for t in out if t.get("rerouted"))
    errors = sum(1 for t in out if (t.get("status") and (str(t.get("status")).startswith("4") or str(t.get("status")).startswith("5"))) or t.get("error"))
    totals = {"requests": requests, "rerouted": rerouted, "errors": errors}
    return {"seq": seq, "totals": totals, "traces": out}


# ---------------------------------------------------------------------------
# 路由组分享 / 导入（对标 Magpie 的 Copy to share / Import）
# ---------------------------------------------------------------------------

def share_group(group_id: str) -> Dict[str, Any]:
    """把路由组导出为可分享文本（不含任何密钥）。"""
    grp = get_group(group_id)
    if not grp:
        return {"ok": False, "detail": "group not found"}
    payload = {
        "magpie": "routing-group",
        "group": {
            "id": grp["id"],
            "name": grp["name"],
            "mode": grp["mode"],
            "fallback": grp["fallback"],
            "affinity": grp["affinity"],
            "sink": grp["sink"],
            "first_token": grp["first_token"],
            "patience": grp["patience"],
            "effort": grp["effort"],
            "levels": grp["levels"],
            "unlists": grp["unlists"],
            "weights": grp["weights"],
            "pick": grp["pick"],
            "members": grp["members"],
        },
    }
    return {"ok": True, "shared": json.dumps(payload, ensure_ascii=False, indent=2)}


def import_group(text: str, replace: bool = False) -> Dict[str, Any]:
    """从分享文本导入路由组（不含密钥，只导入编排）。"""
    try:
        x = json.loads(text)
    except Exception as e:
        return {"ok": False, "detail": f"invalid json: {e}"}
    g = (x or {}).get("group")
    if not isinstance(g, dict) or not g.get("name"):
        return {"ok": False, "detail": "missing group.name"}
    # 同名组：replace=True 时覆盖，否则跳过
    existing = list_groups()
    same = next((x for x in existing if x["name"] == g["name"]), None)
    if same and not replace:
        return {"ok": False, "detail": "group exists", "id": same["id"]}
    if same and replace:
        update_group(same["id"], name=g.get("name", same["name"]),
                     mode=g.get("mode", "smart"),
                     fallback=g.get("fallback", "global_pool"),
                     members=g.get("members", []), affinity=g.get("affinity", "session"),
                     sink=bool(g.get("sink", True)), first_token=g.get("first_token", 0),
                     patience=g.get("patience", 0), effort=g.get("effort", ""),
                     levels=g.get("levels", []), unlists=bool(g.get("unlists", False)),
                     weights=g.get("weights", {}), pick=g.get("pick", ""))
        return {"ok": True, "replaced": True, "id": same["id"], "name": g["name"]}
    created = create_group(
        name=g.get("name"),
        mode=g.get("mode", "smart"),
        fallback=g.get("fallback", "global_pool"),
        members=g.get("members", []),
        affinity=g.get("affinity", "session"),
        sink=bool(g.get("sink", True)),
        first_token=g.get("first_token", 0),
        patience=g.get("patience", 0),
        effort=g.get("effort", ""),
        levels=g.get("levels", []),
        unlists=bool(g.get("unlists", False)),
        weights=g.get("weights", {}),
        pick=g.get("pick", ""))
    return {"ok": True, "replaced": False, "id": created["id"], "name": created["name"]}
