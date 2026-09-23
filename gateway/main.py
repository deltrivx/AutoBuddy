import os
import json
import asyncio
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
import httpx

try:
    from gateway.token_tracker import (record_token_usage, get_aggregated_token_stats,
                                       UsageScanner, extract_usage)
except ImportError:
    from token_tracker import (record_token_usage, get_aggregated_token_stats,
                               UsageScanner, extract_usage)

try:
    from gateway import api_keys
except ImportError:
    import api_keys

try:
    from gateway import model_policy, model_health, account_policy
except ImportError:
    import model_policy
    import model_health
    import account_policy


# ---------------------------------------------------------------------------
# 版本号
#
# 只有一套号：GitHub Release 标签（同时也是镜像 tag）。发版时这里与 CHANGELOG
# 的版本段在同一次提交里一起改，`_test_version_sync.py` 在 CI 里把关，对不上
# 直接构建失败。
#
# 历史上这里是与发布号平行的另一套内部版本（1.x），于是面板显示 v1.8.0、
# 发布页写 v0.4.4 —— 同一份东西两个号，看的人根本没法判断自己跑的是不是最新。
# `AB_VERSION` 环境变量可覆盖（自建镜像 / fork 用得上）。
# ---------------------------------------------------------------------------
VERSION_DEFAULT = "0.5.1"
GATEWAY_VERSION = (os.getenv("AB_VERSION") or "").strip() or VERSION_DEFAULT


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """容器内的账号轮询与模型巡检随应用启停（替代已弃用的 on_event）。"""
    # 回填上一轮巡检摘要：运行状态只在内存里，进程一重启界面就会退化成
    # 「还没有执行过巡检」，而巡检间隔动辄以小时计 —— 用户看到的会是个空白面板。
    last = model_health.load_last_run()
    if last:
        _HEALTH_RUNTIME["lastResult"] = last
        _HEALTH_RUNTIME["lastRunAt"] = last.get("checkedAt")
    task = asyncio.create_task(account_rotate_loop())
    health_task = asyncio.create_task(model_health_loop())
    try:
        yield
    finally:
        task.cancel()
        health_task.cancel()


app = FastAPI(title="AutoBuddy OpenAI Gateway", version=GATEWAY_VERSION, lifespan=lifespan)

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
ACCOUNT_POOL_FILE = DATA_DIR / "account_pool_config.json"
SELECTION_LOG_FILE = DATA_DIR / "selection_logs.json"
# 模型级禁用策略：某些账号的个别模型会因优惠政策调整 / 时效到期而不可调用，
# 需要在**账号 x 模型**这个粒度上拉黑，而不是停用整个账号。
MODEL_POLICY_FILE = DATA_DIR / "model_policy.json"
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://www.codebuddy.ai/v2")
CN_BASE_URL = os.getenv("CN_BASE_URL", "https://copilot.tencent.com/v2")

# 基础模型清单：官方别名与路由模式。这些不一定会出现在 usage 记录里，因此常驻。
# 注意：真实可用模型由 discover_models() 从官方 usage 数据自动补全，不要在这里逐个手工添加新模型。
BASE_MODELS = [
    # 混元
    {"id": "hy3", "name": "Hy3 (混元思考模型)", "owned_by": "tencent-codebuddy"},
    {"id": "hy4", "name": "Hy4 (别名 -> hy3)", "owned_by": "tencent-codebuddy"},
    # DeepSeek
    {"id": "deepseek-v3", "name": "DeepSeek-V3", "owned_by": "deepseek"},
    {"id": "deepseek-chat", "name": "DeepSeek-Chat (别名 -> deepseek-v3)", "owned_by": "deepseek"},
    # Kimi / 月之暗面
    {"id": "kimi-k3", "name": "Kimi-K3", "owned_by": "moonshot"},
    {"id": "kimi-k2.6", "name": "Kimi-K2.6", "owned_by": "moonshot"},
    {"id": "kimi-k2.5", "name": "Kimi-K2.5", "owned_by": "moonshot"},
    {"id": "kimi", "name": "Kimi (别名 -> kimi-k3)", "owned_by": "moonshot"},
    # GPT 系列
    {"id": "gpt-5.5", "name": "GPT-5.5 (OpenAI旗舰编码模型)", "owned_by": "openai"},
    {"id": "gpt-5.4", "name": "GPT-5.4", "owned_by": "openai"},
    {"id": "gpt-5.3-codex", "name": "GPT-5.3-Codex", "owned_by": "openai"},
    {"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol (复杂推理与长程任务)", "owned_by": "openai"},
    {"id": "gpt-5.6-terra", "name": "GPT-5.6-Terra (速度与成本均衡)", "owned_by": "openai"},
    {"id": "gpt-5.6-luna", "name": "GPT-5.6-Luna (轻量极速)", "owned_by": "openai"},
    {"id": "gpt-4o", "name": "GPT-4o (别名 -> gpt-5.4)", "owned_by": "openai"},
    # Gemini 系列
    {"id": "gemini-3.1-pro", "name": "Gemini-3.1-Pro", "owned_by": "google"},
    {"id": "gemini-3.5-flash", "name": "Gemini-3.5-Flash", "owned_by": "google"},
    # GLM 系列
    {"id": "glm-5.3", "name": "GLM-5.3", "owned_by": "zhipu"},
    {"id": "glm-5.2", "name": "GLM-5.2", "owned_by": "zhipu"},
    # MiniMax 系列
    {"id": "minimax-m3", "name": "MiniMax-M3", "owned_by": "minimax"},
    # 官方模式别名
    {"id": "default-model", "name": "Auto (智能路由)", "owned_by": "tencent-codebuddy"},
    {"id": "fast-model", "name": "Fast (快速响应)", "owned_by": "tencent-codebuddy"},
    {"id": "balanced-model", "name": "Balanced (均衡日常)", "owned_by": "tencent-codebuddy"},
    {"id": "primary-model", "name": "Primary (高质量复杂任务)", "owned_by": "tencent-codebuddy"},
    {"id": "deep-model", "name": "Deep (深度推理分析)", "owned_by": "tencent-codebuddy"}
]

# 模型别名表由 model_policy 统一维护（禁用别名要连带挡住目标模型）。
MODEL_ALIAS_MAP = model_policy.MODEL_ALIAS_MAP

# ---------------------------------------------------------------------------
# 模型自动发现
# 官方上游没有 /models 端点（实测 404），模型清单是从账号的实际调用记录里聚合出来的。
# 因此这里直接读取官方 usage 缓存与网关自身的 token 统计，自动补全新出现的模型，
# 避免「官方上新一个模型就要手工往清单里加一个」。
# ---------------------------------------------------------------------------

_OWNER_PREFIXES = [
    ("deepseek", "deepseek"),
    ("gpt", "openai"),
    ("claude", "anthropic"),
    ("gemini", "google"),
    ("glm", "zhipu"),
    ("kimi", "moonshot"),
    ("minimax", "minimax"),
    ("codewise", "tencent-codebuddy"),
    ("hy", "tencent-codebuddy"),
]


def infer_owner(model_id: str) -> str:
    lowered = (model_id or "").lower()
    for prefix, owner in _OWNER_PREFIXES:
        if lowered.startswith(prefix):
            return owner
    return "workbuddy"


def _discover_from_official_usage() -> List[str]:
    """官方 usage 缓存 payload 中记录了账号实际调用过的模型（汇总/明细/按天三个维度）。"""
    cache_file = DATA_DIR / "official_usage_cache.json"
    if not cache_file.exists():
        return []
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            payload = (json.load(f) or {}).get("payload") or {}
    except Exception:
        return []

    names = set()
    for item in payload.get("models") or []:
        if isinstance(item, dict) and item.get("model"):
            names.add(str(item["model"]))
    for item in payload.get("requests") or []:
        if isinstance(item, dict) and item.get("model"):
            names.add(str(item["model"]))
    for day in payload.get("daily") or []:
        for item in (day or {}).get("models") or []:
            if isinstance(item, dict) and item.get("model"):
                names.add(str(item["model"]))
    return sorted(names)


def _discover_from_token_stats() -> List[str]:
    """网关自身记录的 token 统计里出现过的模型名。"""
    log_file = DATA_DIR / "token_stats_logs.json"
    if not log_file.exists():
        return []
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            records = json.load(f)
    except Exception:
        return []
    if not isinstance(records, list):
        return []
    return sorted({str(r["model"]) for r in records if isinstance(r, dict) and r.get("model")})


_MODEL_CACHE: Dict[str, Any] = {"signature": None, "catalog": []}


def _catalog_signature() -> tuple:
    """以相关文件的 mtime + size 作为缓存签名，文件一变即重新发现。"""
    sig = []
    for name in ("official_usage_cache.json", "token_stats_logs.json"):
        try:
            st = (DATA_DIR / name).stat()
            sig.append((name, int(st.st_mtime), st.st_size))
        except Exception:
            sig.append((name, 0, 0))
    return tuple(sig)


def get_model_catalog() -> List[Dict[str, Any]]:
    """基础清单 ∪ 自动发现的模型，按 id 去重（基础清单优先，保留中文说明）。"""
    signature = _catalog_signature()
    if _MODEL_CACHE["signature"] == signature and _MODEL_CACHE["catalog"]:
        return _MODEL_CACHE["catalog"]

    catalog: Dict[str, Dict[str, Any]] = {m["id"]: dict(m) for m in BASE_MODELS}
    discovered = set(_discover_from_official_usage()) | set(_discover_from_token_stats())
    for name in discovered:
        if name in catalog:
            continue
        catalog[name] = {"id": name, "name": name, "owned_by": infer_owner(name)}

    result = list(catalog.values())
    _MODEL_CACHE["signature"] = signature
    _MODEL_CACHE["catalog"] = result
    return result


def get_active_account() -> Optional[Dict[str, Any]]:
    """返回轮询状态里的「当前账号」，仅用于健康状态与兼容旧接口。

    API 请求不再调用这里：请求会走 select_account()，按照账号池配置独立选账号，
    这样多个并发请求可以分配到不同账号，而不是全部卡在 activeAccountId。
    """
    accounts = _load_accounts()
    state_file = DATA_DIR / "rotate" / "state.json"
    active_id = None
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                active_id = (json.load(f) or {}).get("activeAccountId")
        except Exception:
            pass
    if active_id:
        for acc in accounts:
            if acc.get("id") == active_id or acc.get("uid") == active_id:
                return acc
    return accounts[0] if accounts else None


def _load_pool_config() -> Dict[str, Any]:
    """读取账号池配置；不存在时默认所有有 token 的账号启用、自动分配。"""
    default = {
        "mode": "auto",  # auto | manual
        "enabledAccountIds": [],  # 空数组表示所有账号启用
        "manualAccountId": None,
        "updatedAt": None,
    }
    try:
        if ACCOUNT_POOL_FILE.exists():
            with open(ACCOUNT_POOL_FILE, "r", encoding="utf-8") as f:
                value = json.load(f) or {}
            if isinstance(value, dict):
                default.update(value)
    except Exception as e:
        print(f"[account-pool] failed to load config: {e}")
    if default.get("mode") not in ("auto", "manual"):
        default["mode"] = "auto"
    if not isinstance(default.get("enabledAccountIds"), list):
        default["enabledAccountIds"] = []
    return default


def _save_pool_config(config: Dict[str, Any]) -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config["updatedAt"] = int(time.time() * 1000)
    with open(ACCOUNT_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return config


# ---------------------------------------------------------------------------
# 模型级禁用策略（账号 x 模型）
#
# 需求来源（用户 2026-09-19）：账号提供了很多模型，但受优惠政策 / 时效性影响，
# 个别账号的个别模型可能调不通。停用整个账号损失太大，需要的粒度是
# 「这个账号的这一个模型不参与轮询」，其余模型照常。
#
# 策略的读写实现已抽到 model_policy 模块 —— 因为「模型可用性自动巡检」
# 也要读写同一份策略，两处各写一份早晚会漂移。这里只做转发。
# ---------------------------------------------------------------------------

def _load_model_policy() -> Dict[str, List[str]]:
    return model_policy.load_policy()


def _save_model_policy(policy: Dict[str, List[str]]) -> Dict[str, List[str]]:
    return model_policy.save_policy(policy)


def _disabled_models_for(account_id: Optional[str], policy: Optional[Dict[str, List[str]]] = None) -> set:
    return model_policy.disabled_models_for(account_id, policy)


def _model_is_disabled(account_id: Optional[str], model: Optional[str],
                       policy: Optional[Dict[str, List[str]]] = None) -> bool:
    return model_policy.model_is_disabled(account_id, model, policy)


def _account_id(acc: Dict[str, Any]) -> Optional[str]:
    return acc.get("id") or acc.get("uid")


def _account_label(acc: Dict[str, Any]) -> str:
    return acc.get("nickname") or acc.get("email") or acc.get("uid") or acc.get("id") or "未命名账号"


def _enabled_accounts(accounts: List[Dict[str, Any]], config: Dict[str, Any],
                      include_disabled: bool = False) -> List[Dict[str, Any]]:
    """账号池白名单 + 可用性过滤，可选是否叠加「账号级停用」。

    ``include_disabled`` 用于**显式指定**的路径：停用的语义是「不参与自动轮询」，
    而不是「禁止调用」—— 用户想临时用某个已停用的账号兜底时，
    不该逼他先去界面把停用解除掉。
    """
    enabled = {str(x) for x in config.get("enabledAccountIds") or []}
    # 空数组是默认值，语义是全部启用；保存明确列表后才是白名单。
    candidates = accounts if not enabled else [a for a in accounts if str(_account_id(a)) in enabled]
    # 再叠加账号级停用策略（人点的或巡检写的）。两份配置都只能**减少**参与调用的账号，
    # 谁都不许把账号偷偷启用回来 —— 于是「关掉巡检之后手动停用依然生效」是天然成立的，
    # 不需要在两者之间做任何迁移。
    if not include_disabled:
        disabled = account_policy.disabled_ids()
        if disabled:
            candidates = [a for a in candidates if str(_account_id(a)) not in disabled]
    now_ms = int(time.time() * 1000)
    return [a for a in candidates if _account_is_usable(a, now_ms)]


_ACCOUNT_POOL_RUNTIME: Dict[str, Any] = {
    "next_index": 0,
    "last_selected_id": None,
    "last_selected_source": None,
}

# 选账号流水：用于观测「并发请求到底分摊到了哪些账号」。
# 只保留最近 200 条，避免无界增长。
#
# v0.3.18 起**落盘**到 SELECTION_LOG_FILE。此前它只在进程内存里，容器一重建/重启，
# 账号卡片上的「已调用 N 次」就全部归零（用户 2026-09-19 反馈）。
# 注意它与 token_stats_logs.json 的区别只是**窗口大小**（200 vs 3000），
# 两者都是滑动窗口，都不是历史累计值 —— 落盘只是让它跨重启连续，不是变成总数。
_SELECTION_LOG: List[Dict[str, Any]] = []
_SELECTION_LOG_MAX = 200

# 同步路由跑在 FastAPI 的线程池里，next_index 的「读-改-写」不是原子操作。
# 不加锁时两个并发请求会读到同一个 index，双双落到同一账号 —— 表现为
# 「并发时其实只用了其中一个账号」。这里用锁把「取号 + 递增」串起来。
_SELECTION_LOCK = threading.Lock()


def _load_selection_log() -> List[Dict[str, Any]]:
    """从磁盘恢复选账号流水。文件不存在或损坏时返回空列表，绝不抛。"""
    try:
        if not SELECTION_LOG_FILE.exists():
            return []
        with open(SELECTION_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        entries = [e for e in data if isinstance(e, dict) and e.get("accountId")]
        return entries[-_SELECTION_LOG_MAX:]
    except Exception as e:
        print(f"[account-pool] failed to load selection log: {e}", flush=True)
        return []


def _save_selection_log_locked() -> None:
    """把当前流水整份写回磁盘。**必须在持有 _SELECTION_LOCK 时调用。**

    整份重写而不是追加：上限只有 200 条（约 20 KB），比追加更不容易写出半截 JSON。
    用 tmp + os.replace 做原子替换，进程被 kill 也不会留下坏文件。
    异常一律吞掉 —— 统计落盘失败绝不能影响正常的 API 请求。
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SELECTION_LOG_FILE.with_name(SELECTION_LOG_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_SELECTION_LOG, f, ensure_ascii=False)
        os.replace(tmp, SELECTION_LOG_FILE)
    except Exception as e:
        print(f"[account-pool] failed to persist selection log: {e}", flush=True)


_SELECTION_LOG = _load_selection_log()


def _remember_selection(acc: Dict[str, Any], source: str) -> Dict[str, Any]:
    """记录本次选中的账号与来源。

    三条路径（请求指定 / 手动固定 / 自动轮询）都要落记录，否则状态接口里
    lastSelectedAccountId 会停留在上一次自动分配的结果，无法反映真实调用账号。
    """
    account_id = _account_id(acc)
    _ACCOUNT_POOL_RUNTIME["last_selected_id"] = account_id
    _ACCOUNT_POOL_RUNTIME["last_selected_source"] = source

    entry = {
        "ts": int(time.time() * 1000),
        "accountId": account_id,
        "accountName": _account_label(acc),
        "source": source,
    }
    with _SELECTION_LOCK:
        _SELECTION_LOG.append(entry)
        del _SELECTION_LOG[:-_SELECTION_LOG_MAX]
        # 落盘放在锁内：保证「后发生的 append」一定写出更新的整份快照，
        # 不会出现两个线程的快照乱序覆盖。20 KB 的整份写，开销可忽略。
        _save_selection_log_locked()
    # 打日志便于 docker logs 直接核对并发分摊情况
    print(f"[pool] {source:7s} -> {entry['accountName']} ({account_id})", flush=True)
    return acc


def selection_stats(limit: int = 20) -> Dict[str, Any]:
    """汇总选账号流水：各账号命中次数 + 最近若干条明细。"""
    with _SELECTION_LOCK:
        entries = list(_SELECTION_LOG)
    counts = Counter(str(e.get("accountId")) for e in entries)
    names: Dict[str, str] = {}
    for e in entries:
        names.setdefault(str(e.get("accountId")), str(e.get("accountName")))
    return {
        "total": len(entries),
        "distinctAccounts": len(counts),
        "counts": [
            {"accountId": aid, "accountName": names.get(aid), "count": c}
            for aid, c in counts.most_common()
        ],
        "recent": entries[-limit:],
    }


def select_account(requested_id: Optional[str] = None, model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """为每一个 API 请求选择账号。

    - X-AutoBuddy-Account-Id / body.account_id 指定时：手动选指定账号；
    - manual 模式：固定使用配置的 manualAccountId；
    - auto 模式：在 enabledAccountIds 中轮询，每个并发请求可拿到不同账号。

    这里是「请求级并行」：一条对话请求仍由一个账号完成，但多个同时到达的
    独立请求会分摊到多个账号，不会把同一个请求拆成两份导致上下文/计费混乱。

    ``model`` 用于叠加**模型级禁用**：该账号若把本请求要用的模型拉黑了，就不再
    参与本次轮询，转而选择下一个账号。这样「某账号某模型不可用」不会让整个请求失败。

    **账号级停用**只影响自动轮询与手动模式，不影响显式指定 ——
    停用的语义是「轮询时不选它」，而不是「禁止调用」。同理，全部账号都被停用时
    自动轮询会回退到原始候选集，让上游返回真实错误，而不是在网关层编一个「无账号」。
    """
    accounts = _load_accounts()
    config = _load_pool_config()
    candidates = _enabled_accounts(accounts, config)
    # 「显式指定」与「手动模式」走**包含停用账号**的候选集：
    # 停用的语义是不参与自动轮询，而不是禁止调用。人明确点名要用它，
    # 就该照办 —— 否则用户为了临时兜底还得先去界面解一次停用。
    explicit_pool = _enabled_accounts(accounts, config, include_disabled=True)
    explicit_by_id = {str(_account_id(a)): a for a in explicit_pool if _account_id(a)}

    if model:
        policy = _load_model_policy()
        allowed = [a for a in candidates if not _model_is_disabled(_account_id(a), model, policy)]
        # 若所有账号都禁用了该模型，宁可回退到原始候选集也不直接失败 ——
        # 让上游去返回真实错误，比在网关层编一个「无账号」更利于排查。
        if allowed:
            candidates = allowed

    by_id = {str(_account_id(a)): a for a in candidates if _account_id(a)}

    if requested_id:
        # 模型级禁用仍然拦得住显式指定（那是「这个账号这个模型不可用」的硬约束），
        # 账号级停用拦不住（那只针对自动轮询）。
        acc = explicit_by_id.get(str(requested_id))
        if acc and model and _model_is_disabled(_account_id(acc), model, _load_model_policy()):
            return None
        return _remember_selection(acc, "request") if acc else None

    if config.get("mode") == "manual":
        acc = explicit_by_id.get(str(config.get("manualAccountId")))
        return _remember_selection(acc, "manual") if acc else None

    # 全部账号都被账号级停用时，宁可回退到「包含停用账号」的集合也不返回 None：
    # 让上游去返回真实错误，比在网关层编一个「无账号」更利于排查
    # （与模型级全禁用的处理保持一致）。
    if not candidates:
        candidates = explicit_pool
    if not candidates:
        return None
    # 取号与递增必须在同一把锁内完成，否则并发下会重复命中同一账号。
    with _SELECTION_LOCK:
        index = int(_ACCOUNT_POOL_RUNTIME.get("next_index", 0)) % len(candidates)
        _ACCOUNT_POOL_RUNTIME["next_index"] = (index + 1) % len(candidates)
    acc = candidates[index]
    return _remember_selection(acc, "auto")


@app.get("/account-pool/status")
def account_pool_status():
    accounts = _load_accounts()
    config = _load_pool_config()
    enabled_ids = {str(x) for x in config.get("enabledAccountIds") or []}
    now_ms = int(time.time() * 1000)
    stats = selection_stats(limit=10)
    # 账号级停用来源表。界面据此区分「谁停的」——人点的与巡检写的文案不同，
    # 否则「已停用」会被读成用户自己误操作过。
    account_disabled = account_policy.as_source_map()
    return {
        "mode": config.get("mode"),
        "manualAccountId": config.get("manualAccountId"),
        "allEnabledByDefault": not bool(enabled_ids),
        "enabledAccountIds": sorted(enabled_ids),
        "lastSelectedAccountId": _ACCOUNT_POOL_RUNTIME.get("last_selected_id"),
        "lastSelectedSource": _ACCOUNT_POOL_RUNTIME.get("last_selected_source"),
        "selectionCounts": stats["counts"],
        "modelPolicy": model_policy.as_model_lists(_load_model_policy()),
        "accountDisabled": account_disabled,
        "accounts": [
            {
                "id": _account_id(a),
                "name": _account_label(a),
                "variant": a.get("variant"),
                # 账号级停用（独立于账号池白名单），来源单独给出。
                "disabled": str(_account_id(a)) in account_disabled,
                "disabledSource": account_disabled.get(str(_account_id(a))),
                "enabled": not enabled_ids or str(_account_id(a)) in enabled_ids,
                "usable": _account_is_usable(a, now_ms),
                "active": _account_id(a) == (_rotate_state_snapshot().get("active_account_id") or ""),
            }
            for a in accounts
        ],
    }


@app.get("/account-pool/selections")
def account_pool_selections(limit: int = 50):
    """选账号分摊观测：各账号命中次数 + 最近明细。

    用于回答「并发请求是否真的分摊到了多个账号，而不是只用了其中一个」。
    """
    return selection_stats(limit=max(1, min(int(limit), _SELECTION_LOG_MAX)))


@app.post("/account-pool/selections/reset")
def reset_account_pool_selections():
    """清空选账号流水，便于做干净的压测观测。**同时清掉磁盘上的副本**，
    否则下次重启会把旧流水又读回来，看起来像「清零没生效」。"""
    with _SELECTION_LOCK:
        _SELECTION_LOG.clear()
        _save_selection_log_locked()
    return {"ok": True, "total": 0}


@app.put("/account-pool/config")
def update_account_pool(config: Dict[str, Any]):
    """更新账号池配置。

    **未提交的字段保持原值，而不是被重置成默认值。** 这一点是必须的：

    界面上「设为首选」只需要改 mode 与 manualAccountId，它提交的 payload 里
    并没有 enabledAccountIds。若这里用 ``config.get("enabledAccountIds", [])``
    取值，缺字段会被读成空数组并**覆盖落盘** —— 用户的白名单（哪些账号参与
    轮询）在一次「设为首选」后整份丢失。而空数组在业务上表示「全部启用」，
    所以现场看不出任何异常，只在后续排查时表现为「设置没生效」。

    因此这里以**已保存的配置为底**，只覆盖调用方明确提交的字段。
    """
    current = _load_pool_config()

    mode = config.get("mode", current.get("mode", "auto"))
    if mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode must be auto or manual")

    if "enabledAccountIds" in config:
        enabled = config.get("enabledAccountIds")
        if not isinstance(enabled, list):
            raise HTTPException(status_code=400,
                                detail="enabledAccountIds must be an array")
        enabled = [str(x) for x in enabled]
    else:
        # 没提交就沿用原值 —— 这是修复「设为首选把白名单清空」的关键一行。
        enabled = [str(x) for x in (current.get("enabledAccountIds") or [])]

    accounts = _load_accounts()
    known = {str(_account_id(a)) for a in accounts if _account_id(a)}
    unknown = [x for x in enabled if x not in known]
    if unknown:
        raise HTTPException(status_code=400, detail={"unknownAccountIds": unknown})

    if "manualAccountId" in config:
        manual_id = config.get("manualAccountId")
    else:
        manual_id = current.get("manualAccountId")

    # 切到 manual 时必须指名一个**有效的**账号，否则请求会被路由到空处。
    # 未提交 manualAccountId 而当前也没有可用值时，退回 auto 而不是报错 ——
    # 「只想改白名单」的请求不该因为历史配置里没有首选账号而失败。
    if mode == "manual":
        pool = enabled or sorted(known)
        if str(manual_id) not in pool:
            manual_id = None
            mode = "auto"

    saved = _save_pool_config({
        "mode": mode,
        "enabledAccountIds": enabled,
        "manualAccountId": manual_id,
    })
    return {"ok": True, "config": saved, "status": account_pool_status()}


# ---------------------------------------------------------------------------
# 账号级停用：与「账号池白名单」正交的一份独立策略
#
# 为什么不复用账号池的 enabledAccountIds：
#   - 那份是**用户的配置**（保存整份列表），巡检不该去改它；
#   - 这里要记「是谁停的」（manual / auto），数组表达不了来源；
#   - 巡检自动停用需要能自愈，而配置里的白名单若被巡检删项，
#     用户根本看不出自己的配置被动过。
# 于是独立成文件，两份都只能**减少**参与调用的账号。

@app.post("/account-pool/toggle")
def toggle_account_disabled(payload: Dict[str, Any]):
    """停用 / 启用单个账号（人在界面上点的，来源记 manual）。

    停用的语义是「轮询时不选它」，**不是**禁止调用 ——
    显式指定这个账号的请求仍然会被接受（见 ``select_account``），
    这样用户想临时用它兜底时不必先去改配置。
    """
    acc_id = payload.get("accountId")
    if not acc_id:
        raise HTTPException(status_code=400, detail="accountId is required")
    accounts = _load_accounts()
    known = {str(_account_id(a)) for a in accounts if _account_id(a)}
    if str(acc_id) not in known:
        raise HTTPException(status_code=404, detail="account not found")
    disabled = bool(payload.get("disabled", True))
    policy = account_policy.load_policy()
    changed = account_policy.set_disabled(policy, str(acc_id), disabled,
                                          account_policy.SOURCE_MANUAL)
    if changed:
        account_policy.save_policy(policy)
    return {
        "ok": True,
        "changed": changed,
        "accountId": str(acc_id),
        "disabled": disabled,
        "source": account_policy.SOURCE_MANUAL if disabled else None,
        "status": account_pool_status(),
    }


@app.post("/account-health/probe")
def probe_account_credentials(payload: Dict[str, Any]):
    """就地检测一个账号的凭据是否有效（不消耗额度、不改配置）。

    只报告结论。要停用它由用户看着结果自己决定（旁边就是停用按钮）——
    探测结果与改配置分开，是为了不让人在「点了检测」之后发现配置被悄悄改了。
    """
    acc_id = payload.get("accountId")
    if not acc_id:
        raise HTTPException(status_code=400, detail="accountId is required")
    accounts = _load_accounts()
    by_id = {str(_account_id(a)): a for a in accounts if _account_id(a)}
    acc = by_id.get(str(acc_id))
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")

    # 这是一个同步 HTTP 调用，但**不能**直接写在 def 路由里等它 ——
    # FastAPI 的 def 路由跑在线程池里，阻塞的是线程不是事件循环，
    # 所以这里直接同步执行即可（与 run_model_health_check 同一模式）。
    variant = acc.get("variant", "ai")
    base_url = _health_base_url(variant)
    token = acc.get("access_token")
    client = httpx.Client(timeout=30.0)
    try:
        result = model_health.probe_account(client, base_url, token)
    finally:
        try:
            client.close()
        except Exception:
            pass

    verdict = result.get("verdict")
    semantic = result.get("semantic")
    # 结论文案由 model_health 的统一语义表派生，接口这边不再自带一份副本 ——
    # 两份文案各自演化，正是「同一个账号在不同页面口径不同」的老问题来源。
    state = model_health.credential_state(result)
    # 判定依据：优先用语义表里针对**具体错误码**的解释（如 11140 → 内容审查），
    # 语义识别不出时才退回按 verdict 的通用说明。
    #
    # 但**「有效」这一档不给 evidence**：那一档的语义解释就是「凭据有效」，
    # 与 message 同文，拼上去只会得到「凭据有效（凭据有效）」这种绕口令
    # （线上实测踩到过）。有效档改用 method 说明探测手段，措辞也只讲
    # 「用一个不存在的模型名试，上游认下了凭据即有效」，不出现听起来像
    # 故障的「模型不存在」字样。
    evidence = None
    if state.get("state") != "valid":
        evidence = (model_health.SEMANTIC_LABELS.get(semantic or "")
                    or state.get("detail"))
    return {
        "ok": True,
        "accountId": str(acc_id),
        "accountName": _account_label(acc),
        # 保留 verdict 字段兼容旧前端，但含义以 state 为准（见下）。
        "verdict": verdict,
        # 统一的凭据状态：valid / invalid / restricted / unknown。
        # 界面只认这一个字段，不再自行解释 verdict 或状态码。
        "state": state.get("state"),
        "message": state.get("label") or "未识别的探测结论",
        # **界面优先显示这一条**：一句人话，不带任何探测细节。
        # 「账号正常，可以放心使用」/「账号被上游拦截，暂时用不了」——
        # 用户要的是「能不能用、为什么不能用」，不是我们怎么测的。
        # 「重新扫码没用」这类要件完整说明的结论不放这里，它归设置页。
        "userMessage": state.get("userMessage"),
        "evidence": evidence,
        # 探测手段说明（仅「有效」档有值）。**界面默认不展示** ——
        # 它是给排查用的实现细节。曾经把它拼进正常账号的提示里，
        # 结果用户读到的重点是「用一个不存在的模型名试」，像在报故障。
        "method": state.get("method"),
        # 行动建议只从 credential_state 取 —— 这份文案连同 state/label/detail
        # 都在同一个函数里生成，接口不再自己拼一套。
        # 以前就是这么散的：界面一处、接口一处、巡检一处，改一处漏两处，
        # 于是同一个账号在不同位置给出不同说法。
        "action": state.get("action"),
        # 上游错误码原样透出，供排查；但**不**透 HTTP 状态码 ——
        # 探测刻意用一个不存在的模型名，非 2xx 是预期内的，
        # 把状态码摆到界面上只会与「凭据有效」互相矛盾（这一点踩过一次）。
        "code": result.get("code"),
        "semantic": semantic,
        "elapsedMs": result.get("elapsedMs"),
        "error": result.get("error"),
    }


@app.get("/account-health/audit")
def audit_account_health():
    """对全部账号做一次一致性自检，逐账号给出「凭据」与「模型」两侧的结论。

    为什么需要它：凭据有效性与模型可用性是**两个独立维度**，
    但界面上过去只有一个笼统的「账号状态」，于是同一批账号在不同页面
    呈现出互相矛盾的结论（检测说有效、巡检说失效），让人无从判断该信哪个。
    这个接口把两侧结论并排摆出来，并标明每一侧各自的判据，
    使「凭据好但模型被拦」「某模型无权限」这类情况一眼可见。

    只读，不改任何配置 —— 排查工具不该有副作用。
    """
    accounts = _load_accounts()
    pool_cfg = _load_pool_config()
    enabled_ids = {str(x) for x in pool_cfg.get("enabledAccountIds") or []}
    acc_disabled = account_policy.load_policy()
    now_ms = int(time.time() * 1000)

    # 每个账号要测的模型：取该账号被显式禁用之外的、配置里声明过的模型。
    # 上限截断，避免一个接口调用打出几百次上游请求。
    try:
        base_models = _base_model_ids()
    except Exception:
        base_models = []
    max_models = 3

    rows: List[Dict[str, Any]] = []
    client = httpx.Client(timeout=30.0)
    try:
        for acc in accounts:
            acc_id = str(_account_id(acc))
            if not acc_id:
                continue
            variant = acc.get("variant", "ai")
            base_url = _health_base_url(variant)
            token = acc.get("access_token")
            entry = model_policy._coerce_entry(_load_model_policy().get(acc_id)) or {}
            disabled_models = set(entry)

            # 侧一：凭据
            probe = model_health.probe_account(client, base_url, token)
            cred = model_health.credential_state(probe)

            # 侧二：模型（最多 max_models 个未禁用的）
            candidates = [m for m in base_models
                          if m not in disabled_models
                          and model_policy.MODEL_ALIAS_MAP.get(m, m) not in disabled_models]
            model_rows = []
            for model in candidates[:max_models]:
                item = model_health.probe_once(client, base_url, token, model)
                model_rows.append({
                    "model": model,
                    "verdict": item.get("verdict"),
                    "semantic": item.get("semantic"),
                    "code": item.get("code"),
                    "explain": model_health.SEMANTIC_LABELS.get(
                        item.get("semantic") or "", ""
                    ) or {
                        "available": "调用链路正常",
                        "unavailable": "该模型对此账号不可用",
                        "transient": "临时异常，未能判定",
                    }.get(item.get("verdict"), ""),
                })

            # 一致性结论：把两侧摆在一起，直接给出「该怎么理解」。
            verdicts = {r["verdict"] for r in model_rows}
            if cred["state"] == "invalid":
                consistency = "凭据失效"
                advice = "先重新登录该账号；凭据修好之前，它名下模型的探测结果都不代表模型好坏。"
            elif cred["state"] == "restricted":
                consistency = "账号已被上游拦截"
                advice = ("登录是好的，但请求被上游直接拦下，所以用不了。"
                          "重新扫码登录没用（实测：换新凭据后依然被拦，"
                          "上游认的是账号本身），建议换个账号；巡检会自动停用它。")
            elif cred["state"] == "unknown":
                consistency = "凭据未判定"
                advice = "凭据探测未得到结论，模型侧的结论仅供参考。"
            elif model_rows and verdicts == {"available"}:
                consistency = "一致：均正常"
                advice = "凭据与模型两侧都正常，无需处理。"
            elif "restricted" in verdicts:
                consistency = "账号已被上游拦截"
                advice = "登录是好的，但模型请求被上游直接拦下，同上处理。"
            elif model_rows and "unavailable" in verdicts:
                consistency = "凭据有效，部分模型不可用"
                advice = "凭据没问题，是这些模型本身对该账号不可用（无权限 / 不存在），可单独禁用它们。"
            else:
                consistency = "结论不完整"
                advice = "本轮探测未覆盖到足以判定的组合，可稍后重试。"

            rows.append({
                "id": acc_id,
                "name": _account_label(acc),
                "variant": variant,
                "inPool": (not enabled_ids) or acc_id in enabled_ids,
                "usable": _account_is_usable(acc, now_ms),
                "accountDisabled": acc_id in acc_disabled,
                "accountDisabledSource": acc_disabled.get(acc_id),
                "credential": cred,
                "models": model_rows,
                "consistency": consistency,
                "advice": advice,
            })
    finally:
        try:
            client.close()
        except Exception:
            pass

    # 汇总：让调用方一眼看出「哪些账号需要关注」。
    summary: Dict[str, int] = {}
    for row in rows:
        summary[row["consistency"]] = summary.get(row["consistency"], 0) + 1
    return {
        "checkedAt": int(time.time() * 1000),
        "accounts": len(rows),
        "summary": summary,
        "criteria": {
            "credential": "用一个不存在的模型名探测：上游回「模型不存在」= 登录正常；"
                          "回鉴权类错误 = 登录过期；回 403/11140 = 账号被上游拦截"
                          "（登录是好的，但换新凭据也解不开）。",
            "model": "用真实模型名探测：只依据上游错误码判定，"
                     "内容审查与参数错误不会被算作模型不可用。",
            "note": "凭据有效性只由账号级探测裁定，不从模型探测结果反推。",
        },
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 模型级禁用：账号 x 模型
#
# 与账号池配置是**正交**的两件事：
#   - 账号池决定「这个账号参不参与调用」；
#   - 模型策略决定「这个账号的哪些模型不参与调用」。
# 因此单独一个文件、单独一组接口，互不覆盖。
# ---------------------------------------------------------------------------

@app.get("/account-models/config")
def account_models_config():
    """返回模型禁用策略，供账号卡片渲染标签的禁用态。

    同时给出两种视图：
      - ``policy``：``{账号: [模型]}``，只管模型名，保持既有线格式不变；
      - ``policySources``：``{账号: {模型: "manual"|"auto"}}``，让卡片能区分
        「人手禁的」和「巡检禁的」—— 两者的恢复方式与含义并不相同。
    """
    policy = _load_model_policy()
    return {
        "ok": True,
        "policy": model_policy.as_model_lists(policy),
        "policySources": model_policy.as_source_map(policy),
        "disabledTotal": model_policy.disabled_total(policy),
        "disabledBySource": model_policy.count_by_source(policy),
    }


@app.put("/account-models/config")
def update_account_models_config(payload: Dict[str, Any]):
    """切换某账号某个模型的禁用状态。**这里的写入一律标记为手动（manual）。**

    入参两种写法，都支持：
      - 精确式：``{"accountId": "...", "model": "hy3", "disabled": true}``
      - 覆盖式：``{"accountId": "...", "models": ["hy3", "kimi-k3"]}``（整份替换该账号列表）

    ``disabled=False`` 且该模型不在列表里时是无副作用的空操作，直接返回成功，
    避免前端连点造成 4xx。

    写入时打上 ``manual`` 标记，从而使该条目免受巡检「自动启用」的放开 ——
    人点出来的禁用代表明确意图（常见于「能跑但太贵」），不该被自愈抹掉。
    """
    account_id = payload.get("accountId")
    if not account_id:
        raise HTTPException(status_code=400, detail="accountId is required")
    account_id = str(account_id)

    accounts = _load_accounts()
    known = {str(_account_id(a)) for a in accounts if _account_id(a)}
    if account_id not in known:
        raise HTTPException(status_code=400, detail={"unknownAccountId": account_id})

    policy = _load_model_policy()
    if "models" in payload:
        models = payload.get("models")
        if not isinstance(models, list):
            raise HTTPException(status_code=400, detail="models must be an array")
        current = model_policy.replace_account_models(
            policy, account_id, [str(m) for m in models if m],
            source=model_policy.SOURCE_MANUAL)
    else:
        model = payload.get("model")
        if not model:
            raise HTTPException(status_code=400, detail="model is required")
        current = model_policy.set_model_disabled(
            policy, account_id, str(model), bool(payload.get("disabled")),
            source=model_policy.SOURCE_MANUAL)

    _save_model_policy(policy)

    return {
        "ok": True,
        "accountId": account_id,
        "disabledModels": current,
        "disabledSources": model_policy.as_source_map(policy).get(account_id, {}),
        "disabledTotal": model_policy.disabled_total(policy),
    }


@app.post("/account-models/restore")
def restore_account_models(payload: Dict[str, Any]):
    """一键恢复某账号被禁用的模型。

    入参：``{"accountId": "...", "scope": "all" | "auto"}``

    - ``all``（默认）：**不分来源**全部恢复 —— 手动禁用的和巡检禁用的都放回。
      界面上的「全部恢复」按钮即此语义：用户的诉求是「让这些模型重新参与调用」，
      当初是谁禁的并不重要。
    - ``auto``：只恢复巡检写的那些，保留人工禁用的决策。用于「巡检误禁了一批，
      但别动我手动关掉的」。

    落盘前留底：恢复会改变路由行为，出问题时需要能对照恢复了哪些。
    """
    account_id = payload.get("accountId")
    if not account_id:
        raise HTTPException(status_code=400, detail="accountId is required")
    account_id = str(account_id)

    accounts = _load_accounts()
    known = {str(_account_id(a)) for a in accounts if _account_id(a)}
    if account_id not in known:
        raise HTTPException(status_code=400, detail={"unknownAccountId": account_id})

    scope = str(payload.get("scope") or "all")
    if scope not in ("all", "auto"):
        raise HTTPException(status_code=400, detail="scope must be all or auto")

    policy = _load_model_policy()
    sources = None if scope == "all" else [model_policy.SOURCE_AUTO]
    restored = model_policy.enable_all_models(policy, account_id, sources=sources)

    if restored:
        model_policy.backup_policy("before-restore")
        _save_model_policy(policy)

    return {
        "ok": True,
        "accountId": account_id,
        "scope": scope,
        # 本次恢复的模型名，界面据此回显「恢复了哪几个」。
        "restored": restored,
        "restoredCount": len(restored),
        # 恢复后该账号**仍然**被禁用的（scope=auto 时剩下的手动项）。
        # 统一成排序列表：同一字段在 PUT /account-models/config 里也是列表，
        # 一个返回 set() 一个返回 [] 会让前端要写两种判空 —— 接口契约该是一份。
        "disabledModels": sorted(model_policy.disabled_models_for(account_id, policy)),
        "disabledSources": model_policy.as_source_map(policy).get(account_id, {}),
        "disabledTotal": model_policy.disabled_total(policy),
    }


# ---------------------------------------------------------------------------
# 网关的两个核心能力之一是「对外提供 OpenAI 兼容 API」，因此「调用地址 + 密钥」
# 必须能在设置页直接看到、直接改。密钥校验是可选的（默认关闭），开启后 /v1/* 需要
# Authorization: Bearer <key> 或 x-api-key: <key>。
# ---------------------------------------------------------------------------


# 项目元信息。设置页的「关于」面板由此渲染 —— 放在环境变量里而不是写死在前端，
# 一是保持单一出处，二是 fork 出去的人可以改成自己的仓库，
# 不会把使用者引回上游作者的项目。
PROJECT_URL = os.getenv("AB_PROJECT_URL", "https://github.com/deltrivx/AutoBuddy")
PROJECT_NAME = os.getenv("AB_PROJECT_NAME", "AutoBuddy")

# 容器内的 WebUI 代理与网关同容器，靠 loopback 访问。容器外的请求经 docker NAT
# 进来源地址是网桥地址而非 127.0.0.1，所以放行 loopback 不会把外部请求放进来。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def _client_is_loopback(request: Request) -> bool:
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "")
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    # ::ffff:127.0.0.1 这类 IPv4-mapped 形式
    return host.startswith("127.") or host.endswith(":127.0.0.1")


def _extract_api_key(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or request.headers.get("api-key")


def _gateway_auth(request: Request) -> Dict[str, Any]:
    """统一的 /v1 访问校验。返回 ``{"ok": bool, ...}``，失败时由调用方转 401。"""
    state = api_keys.get_state()
    if not state.get("requireKey"):
        return {"ok": True, "mode": "open", "requireKey": False}
    if _client_is_loopback(request):
        return {"ok": True, "mode": "internal", "requireKey": True}
    return api_keys.authenticate(_extract_api_key(request))


def _gateway_info() -> Dict[str, Any]:
    accounts = _load_accounts()
    now_ms = int(time.time() * 1000)
    catalog = get_model_catalog()
    config = _load_pool_config()
    keys = api_keys.get_config()
    enabled = _enabled_accounts(accounts, config)
    return {
        "version": GATEWAY_VERSION,
        "basePath": "/v1",
        "project": {
            "name": PROJECT_NAME,
            "url": PROJECT_URL,
            "releases": f"{PROJECT_URL}/releases",
            "changelog": f"{PROJECT_URL}/blob/main/CHANGELOG.md",
            "issues": f"{PROJECT_URL}/issues",
        },
        "dataDir": str(DATA_DIR),
        "ports": {"gateway": 18091, "console": 18090},
        "endpoints": {
            "chatCompletions": "/v1/chat/completions",
            "models": "/v1/models",
            "health": "/health",
        },
        "auth": {
            "requireKey": keys["requireKey"],
            "header": "Authorization: Bearer <key>",
            "altHeader": "x-api-key: <key>",
            "keysTotal": keys["stats"]["total"],
            "keysEnabled": keys["stats"]["enabled"],
        },
        "models": {
            "count": len(catalog),
            "autoDiscovered": len(catalog) - len(BASE_MODELS),
            "sample": [m["id"] for m in catalog[:12]],
            "disabledCombos": model_policy.disabled_total(),
        },
        "disabledBySource": model_policy.count_by_source(),
        "accounts": {
            "total": len(accounts),
            "usable": len([a for a in accounts if _account_is_usable(a, now_ms)]),
            "inPool": len(enabled),
            "mode": config.get("mode"),
        },
        "features": {
            "stream": True,
            "accountPinHeader": "X-AutoBuddy-Account-Id",
            "accountPinBody": "account_id",
            "aliasRouting": True,
        },
    }


@app.get("/gateway/info")
def gateway_info():
    """连接信息面板的数据源：地址、端点、模型/账号规模、密钥状态。"""
    return _gateway_info()


@app.get("/api-keys/status")
def api_keys_status():
    info = api_keys.get_config()
    info["gateway"] = _gateway_info()
    return info


@app.post("/api-keys")
def api_keys_create(payload: Optional[Dict[str, Any]] = None):
    record, error = api_keys.create_key((payload or {}).get("name"))
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"ok": True, "key": record, "status": api_keys_status()}


def _guard_last_enabled_key(key_id: str) -> None:
    """挡住「把最后一个可用密钥停用/删除」这种把自己锁在门外的操作。

    与「零密钥时不允许开启校验」是同一类防线：一旦开启校验又没有任何启用中的密钥，
    所有下游客户端立刻全部 401，而且只能进容器手改文件才能救回来。
    """
    if not api_keys.get_state().get("requireKey"):
        return
    others = [
        k for k in api_keys.get_config()["keys"]
        if k["enabled"] and str(k["id"]) != str(key_id)
    ]
    if not others:
        raise HTTPException(
            status_code=400,
            detail="这是最后一个启用中的密钥，且已开启密钥校验。"
                   "停用或删除会让所有下游客户端立即失去访问权限；请先关闭密钥校验，或先新建另一个密钥。")


@app.post("/api-keys/update")
def api_keys_update(payload: Dict[str, Any]):
    key_id = payload.get("id")
    if not key_id:
        raise HTTPException(status_code=400, detail="id is required")
    patch = {k: payload[k] for k in ("name", "enabled") if k in payload}
    if not patch:
        raise HTTPException(status_code=400, detail="nothing to update")
    if patch.get("enabled") is False:
        _guard_last_enabled_key(str(key_id))
    record, error = api_keys.update_key(str(key_id), patch)
    if error:
        raise HTTPException(status_code=404, detail=error)
    return {"ok": True, "key": record, "status": api_keys_status()}


@app.post("/api-keys/delete")
def api_keys_delete(payload: Dict[str, Any]):
    key_id = payload.get("id")
    if not key_id:
        raise HTTPException(status_code=400, detail="id is required")
    _guard_last_enabled_key(str(key_id))
    if not api_keys.delete_key(str(key_id)):
        raise HTTPException(status_code=404, detail="key not found")
    return {"ok": True, "status": api_keys_status()}


@app.post("/api-keys/delete-all")
def api_keys_delete_all():
    """一键清空。仅用于「重置成未设防状态」，前端会二次确认。

    开着密钥校验时不允许清空：清完就没人能调了。要重置请先关掉校验 —— 这里选择
    显式报错而不是「顺手帮你把校验关掉」，因为静默降低安全性比多一次点击更糟。
    """
    if api_keys.get_state().get("requireKey"):
        raise HTTPException(
            status_code=400,
            detail="已开启密钥校验，清空全部密钥会让所有下游客户端立即失去访问权限。请先关闭密钥校验。")
    removed = api_keys.delete_all_keys()
    return {"ok": True, "removed": removed, "status": api_keys_status()}


@app.put("/api-keys/config")
def api_keys_config(payload: Dict[str, Any]):
    if "requireKey" not in payload:
        raise HTTPException(status_code=400, detail="requireKey is required")
    require = bool(payload.get("requireKey"))
    if require and api_keys.get_config()["stats"]["enabled"] == 0:
        # 开了校验却一个可用密钥都没有 = 把自己锁在门外
        raise HTTPException(status_code=400,
                            detail="请先创建至少一个可用密钥，再开启密钥校验。")
    api_keys.set_require_key(require)
    return {"ok": True, "status": api_keys_status()}


@app.get("/health")
def health():
    acc = get_active_account()
    catalog = get_model_catalog()
    return {
        "status": "healthy",
        "version": GATEWAY_VERSION,
        "has_active_account": acc is not None,
        "active_account_variant": acc.get("variant") if acc else None,
        "models_count": len(catalog),
        "models_auto_discovered": len(catalog) - len(BASE_MODELS),
        "require_api_key": api_keys.get_state().get("requireKey"),
        "rotate": _rotate_state_snapshot()
    }

@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request):
    gate = _gateway_auth(request)
    if not gate["ok"]:
        raise HTTPException(status_code=401, detail=gate.get("detail") or "Unauthorized")
    if gate.get("keyId"):
        api_keys.mark_used(gate["keyId"])
    catalog = get_model_catalog()
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "created": 1789680000,
                "owned_by": m["owned_by"],
                "permission": []
            }
            for m in catalog
        ]
    }

def normalize_messages_for_upstream(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not messages:
        return [{"role": "system", "content": "You are a helpful assistant."}]
    first = messages[0]
    if first.get("role") != "system":
        return [{"role": "system", "content": "You are a helpful assistant."}] + messages
    return messages

def estimate_tokens_from_chars(char_count: int) -> int:
    """按 3.5 字符 ≈ 1 token 的粗估。只在拿不到上游 usage 时兜底。"""
    if char_count <= 0:
        return 0
    return max(1, int(char_count / 3.5))


def estimate_tokens(text: str) -> int:
    return estimate_tokens_from_chars(len(text or ""))

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    start_time = time.time()

    # 访问密钥校验（默认关闭）。开启后 /v1/* 需携带 Authorization: Bearer <key>
    # 或 x-api-key: <key>；容器内 WebUI 代理走 loopback 免校验，不会被自己的密钥挡住。
    gate = _gateway_auth(request)
    if not gate["ok"]:
        raise HTTPException(status_code=401, detail=gate.get("detail") or "Unauthorized")
    if gate.get("keyId"):
        api_keys.mark_used(gate["keyId"])

    body = await request.json()

    # 请求级账号选择：显式 header/body 优先，其次按 account-pool 配置自动分配。
    # 这让多个并发请求可以并行落到多个账号，而不再全部使用 activeAccountId。
    requested_account_id = (
        request.headers.get("x-autobuddy-account-id")
        or body.get("account_id")
        or body.get("_account_id")
    )
    body.pop("account_id", None)
    body.pop("_account_id", None)

    # 模型要在选账号**之前**取出：模型级禁用是「账号 x 模型」维度的，
    # 选账号时必须知道本次请求要用的模型，才能跳过拉黑了该模型的账号。
    raw_model = body.get("model", "hy3")
    target_model = MODEL_ALIAS_MAP.get(raw_model, raw_model)

    acc = select_account(requested_account_id, model=raw_model)
    if not acc or not acc.get("access_token"):
        if requested_account_id:
            raise HTTPException(status_code=409, detail="Requested WorkBuddy account is disabled, expired, or unavailable.")
        raise HTTPException(status_code=401, detail="No enabled WorkBuddy/CodeBuddy account available in gateway.")

    token = acc["access_token"]
    variant = acc.get("variant", "ai")
    base_url = AI_BASE_URL if variant == "ai" else CN_BASE_URL

    # 归因信息：写入 token 流水，供账号卡片与「用量分布/消耗最高的调用」使用。
    served_account_id = _account_id(acc) or ""
    served_account_name = _account_label(acc)

    requested_stream = body.get("stream", False)

    body["model"] = target_model

    prompt_text = ""
    if "messages" in body and isinstance(body["messages"], list):
        body["messages"] = normalize_messages_for_upstream(body["messages"])
        prompt_text = " ".join([str(m.get("content", "")) for m in body["messages"]])

    input_tokens = estimate_tokens(prompt_text)

    body["stream"] = True

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    client = httpx.AsyncClient(timeout=180.0)

    if requested_stream:
        req = client.build_request("POST", f"{base_url}/chat/completions", json=body, headers=headers)
        res = await client.send(req, stream=True)

        async def stream_generator():
            # 逐块扫描而非缓冲：先 yield 把字节送出去，再解析这一块。
            # 只留「最后一行没读完」的残余，正文只数字符不存内容。
            scanner = UsageScanner()
            try:
                async for chunk in res.aiter_bytes():
                    yield chunk
                    scanner.feed(chunk)
            finally:
                await res.aclose()
                await client.aclose()
                duration = time.time() - start_time
                usage = scanner.usage
                # 拿不到 usage 只有一种情况：客户端中途断开、末帧没到。
                # 此时回退到字符估算，并把缓存命中记为 0（不知道就是不知道）。
                record_token_usage(
                    model=raw_model,
                    input_tokens=usage["prompt"] if usage else input_tokens,
                    output_tokens=(usage["completion"] if usage
                                   else estimate_tokens_from_chars(scanner.text_chars)),
                    duration_sec=duration,
                    request_id=scanner.response_id or "chatcmpl-wb",
                    account_id=served_account_id,
                    account_name=served_account_name,
                    variant=variant,
                    cache_read=usage["cacheRead"] if usage else 0,
                    cache_write=usage["cacheWrite"] if usage else 0,
                )

        return StreamingResponse(
            stream_generator(),
            status_code=res.status_code,
            headers={k: v for k, v in res.headers.items() if k.lower() in ["content-type", "cache-control", "x-accel-buffering"]}
        )
    else:
        req = client.build_request("POST", f"{base_url}/chat/completions", json=body, headers=headers)
        res = await client.send(req, stream=True)
        if res.status_code != 200:
            content = await res.aread()
            await res.aclose()
            await client.aclose()
            return Response(content=content, status_code=res.status_code, headers={"Content-Type": "application/json"})

        collected_content = ""
        response_id = "chatcmpl-wb"
        finish_reason = "stop"
        usage = None

        try:
            async for line in res.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    if "id" in chunk:
                        response_id = chunk["id"]
                    found = extract_usage(chunk)
                    if found:
                        usage = found
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            collected_content += delta["content"]
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
                except Exception:
                    pass
        finally:
            await res.aclose()
            await client.aclose()

        duration = time.time() - start_time
        record_token_usage(
            model=raw_model,
            input_tokens=usage["prompt"] if usage else input_tokens,
            output_tokens=(usage["completion"] if usage else estimate_tokens(collected_content)),
            duration_sec=duration,
            request_id=response_id,
            account_id=served_account_id,
            account_name=served_account_name,
            variant=variant,
            cache_read=usage["cacheRead"] if usage else 0,
            cache_write=usage["cacheWrite"] if usage else 0,
        )
        input_tokens = usage["prompt"] if usage else input_tokens
        output_tokens = usage["completion"] if usage else estimate_tokens(collected_content)

        result_payload = {
            "id": response_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": raw_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": collected_content
                    },
                    "finish_reason": finish_reason
                }
            ],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                # 上游返回了多少就带多少，拿不到就不给 —— 调用方按标准字段读，
                # 多出来的这些只是把缓存命中透明地透出去。
                **({"prompt_tokens_details": {"cached_tokens": usage["cacheRead"]},
                    "cache_read_input_tokens": usage["cacheRead"],
                    "cache_creation_input_tokens": usage["cacheWrite"]} if usage else {})
            }
        }
        return Response(
            content=json.dumps(result_payload, ensure_ascii=False),
            status_code=200,
            headers={"Content-Type": "application/json"}
        )

# ---------------------------------------------------------------------------
# 容器适配的账号轮询
# 官方自带的轮询在需要切换时，会重启桌面应用（/usr/bin/workbuddy）并把认证文件
# 写进 .local/share/CodeBuddyExtension —— 这些宿主资源容器里都没有，切换动作必然失败。
# 但网关对外提供服务只关心「当前用哪个账号」，所以这里实现一套纯容器内的轮询：
# 只维护 rotate/state.json 里的 activeAccountId，不触碰任何桌面应用。
# ---------------------------------------------------------------------------

ROTATE_ENABLED = os.getenv("GATEWAY_ROTATE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
ROTATE_INTERVAL_MINUTES = max(1, int(os.getenv("GATEWAY_ROTATE_INTERVAL_MINUTES", "30") or 30))
ROTATE_LOG_FILE = DATA_DIR / "gateway_rotate_logs.json"

_ROTATE_RUNTIME: Dict[str, Any] = {
    "enabled": ROTATE_ENABLED,
    "interval_minutes": ROTATE_INTERVAL_MINUTES,
    "last_check_at": None,
    "last_switch_at": None,
    "last_reason": None,
    "active_account_id": None,
    "active_account_name": None,
}


def _rotate_state_snapshot() -> Dict[str, Any]:
    return dict(_ROTATE_RUNTIME)


def _load_accounts() -> List[Dict[str, Any]]:
    accounts_file = DATA_DIR / "accounts.json"
    if not accounts_file.exists():
        return []
    try:
        with open(accounts_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_active_account(account_id: str) -> None:
    state_file = DATA_DIR / "rotate" / "state.json"
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state: Dict[str, Any] = {}
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f) or {}
            except Exception:
                state = {}
        state["activeAccountId"] = account_id
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[rotate] failed to persist active account: {e}")


def _append_rotate_log(entry: Dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        logs = []
        if ROTATE_LOG_FILE.exists():
            try:
                with open(ROTATE_LOG_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f) or []
            except Exception:
                logs = []
        logs.append(entry)
        with open(ROTATE_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs[-200:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[rotate] failed to write rotate log: {e}")


def _account_is_usable(acc: Dict[str, Any], now_ms: int) -> bool:
    if not acc.get("access_token"):
        return False
    expires_at = acc.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at > 0 and expires_at <= now_ms:
        return False
    return True


def rotate_once() -> Dict[str, Any]:
    """执行一次轮询：当前账号仍可用就保持不动，失效才切到备用账号。"""
    now_ms = int(time.time() * 1000)
    accounts = _load_accounts()
    usable = [a for a in accounts if _account_is_usable(a, now_ms)]
    _ROTATE_RUNTIME["last_check_at"] = now_ms

    if not usable:
        reason = "没有可用账号（token 缺失或已过期）"
        _ROTATE_RUNTIME["last_reason"] = reason
        _append_rotate_log({"ts": now_ms, "action": "skipped", "reason": reason})
        return {"status": "skipped", "reason": reason}

    current = get_active_account()
    current_id = (current or {}).get("id")
    current_usable = current is not None and any(a.get("id") == current_id for a in usable)

    if current_usable:
        reason = "当前账号仍可用，保持不切换"
        _ROTATE_RUNTIME["last_reason"] = reason
        _ROTATE_RUNTIME["active_account_id"] = current_id
        _ROTATE_RUNTIME["active_account_name"] = current.get("nickname")
        _append_rotate_log({"ts": now_ms, "action": "kept", "reason": reason,
                            "accountId": current_id, "accountName": current.get("nickname")})
        return {"status": "kept", "reason": reason, "activeAccountId": current_id}

    target = usable[0]
    reason = "原账号已失效，切换到可用账号"
    _write_active_account(target.get("id"))
    _ROTATE_RUNTIME["last_switch_at"] = now_ms
    _ROTATE_RUNTIME["last_reason"] = reason
    _ROTATE_RUNTIME["active_account_id"] = target.get("id")
    _ROTATE_RUNTIME["active_account_name"] = target.get("nickname")
    _append_rotate_log({"ts": now_ms, "action": "switched", "reason": reason,
                        "from": current_id, "to": target.get("id"),
                        "accountName": target.get("nickname")})
    return {"status": "switched", "reason": reason,
            "activeAccountId": target.get("id"),
            "activeAccountName": target.get("nickname")}


async def account_rotate_loop() -> None:
    if not ROTATE_ENABLED:
        print("[rotate] gateway-side rotation disabled (GATEWAY_ROTATE_ENABLED)")
        return
    print(f"[rotate] gateway-side rotation enabled, interval={ROTATE_INTERVAL_MINUTES} min")
    while True:
        try:
            result = rotate_once()
            print(f"[rotate] {result.get('status')}: {result.get('reason')}")
        except Exception as e:
            print(f"[rotate] loop error: {e}")
        await asyncio.sleep(ROTATE_INTERVAL_MINUTES * 60)


@app.get("/rotate/status")
def rotate_status():
    return _rotate_state_snapshot()


@app.post("/rotate/run")
def rotate_run():
    return rotate_once()


# ---------------------------------------------------------------------------
# 模型可用性自动巡检
#
# 需求来源（用户 2026-09-19）：在设置里能开关一个轮询，开启后每隔一段时间检测
# 每个账号（或指定账号）对应模型的可用性，不可用就自动禁用、恢复可用就自动启用；
# 关掉之后仍然可以手动禁用。
#
# 实现要点：
# - 巡检读写的就是 model_policy.json —— 与手动禁用是同一份策略，
#   所以「关掉巡检后手动禁用照常有效」，自动写入的禁用项也不会凭空消失。
# - 禁用项带来源标记（manual / auto）：自动启用**只放开自己写的 auto 项**，
#   人手禁的模型不受影响 —— 否则自愈会把「能跑但太贵」这类控成本配置一并放开。
# - 探测会真实发起一次极小的上游请求，因此**默认关闭**，且默认只探测
#   「该账号调用过的模型 + 内置基础清单」，避免一上来就把上游打爆。
# - 探测请求体必须与网关转发上游的形态一致（流式 + 首条为 system +
#   不带任何可被校验的可选参数），否则会被上游整体拒绝，
#   表现为「所有模型都不可用」并成批误禁。
# - 限流（429）与网络异常计为 transient、被上游参数校验拒绝计为
#   probe_defect，两者**都不参与自动禁用**；账号凭据整体失效时跳过该账号；
#   整轮无一个可用时判定为探测异常，整轮作废不写策略。
# ---------------------------------------------------------------------------

_HEALTH_RUNTIME: Dict[str, Any] = {
    "running": False,
    "lastRunAt": None,
    "lastResult": None,
    "lastError": None,
}
# 同一时刻只允许一轮巡检：手动「立即巡检」与后台定时轮次可能撞车，
# 并发跑会重复探测同一个组合，还会让两轮各自读到的策略互相覆盖。
_HEALTH_RUN_LOCK = threading.Lock()


def _used_models_by_account() -> Dict[str, set]:
    """按账号聚合「调用过哪些模型」，供 onlyUsedModels 收窄探测范围。

    数据源是网关自己的 token 流水（``token_stats_logs.json``），
    它带 ``accountId``，能精确归因；官方 usage 缓存只对当前账号返回明细，不适用。
    """
    log_file = DATA_DIR / "token_stats_logs.json"
    if not log_file.exists():
        return {}
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            records = json.load(f)
    except Exception:
        return {}
    if not isinstance(records, list):
        return {}

    result: Dict[str, set] = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        acc = r.get("accountId")
        model = r.get("model")
        if acc and model:
            result.setdefault(str(acc), set()).add(str(model))
    return result


def _base_model_ids() -> List[str]:
    """内置基础模型清单（不含自动发现的），作为「新账号还没调用记录」时的兜底范围。"""
    return [str(m["id"]) for m in BASE_MODELS if m.get("id")]


def _health_base_url(variant: str) -> str:
    return AI_BASE_URL if variant == "ai" else CN_BASE_URL


def _health_problem_note(summary: Dict[str, Any]) -> Optional[str]:
    """把「本轮结果不可信」的原因汇总成一句话；一切正常时返回 None。

    这几类情况必须显式说出来，否则界面上只会显示「一轮下来什么都没变」，
    用户根本无从判断是真的没变化，还是探测压根没跑出有效结论。
    """
    if summary.get("aborted"):
        return summary.get("reason") or "本轮巡检已作废，未写入任何变更。"

    counts = summary.get("counts") or {}
    notes: List[str] = []
    defects = int(counts.get("probe_defect") or 0)
    if defects:
        codes = "、".join(str(c) for c in (summary.get("defectCodes") or [])) or "未知"
        notes.append(
            f"{defects} 个组合的探测请求被上游参数校验拒绝（错误码 {codes}），已跳过未写入"
        )
    auth_failed = summary.get("authFailed") or []
    if auth_failed:
        names = "、".join(str(a.get("name") or a.get("id")) for a in auth_failed[:5])
        notes.append(f"{len(auth_failed)} 个账号凭据失效（{names}），已跳过，建议重新登录")
    return "；".join(notes) if notes else None


def health_config_snapshot() -> Dict[str, Any]:
    cfg = model_health.load_config()
    accounts = _load_accounts()
    wanted = set(cfg.get("accountIds") or [])
    selected = [
        {
            "id": str(_account_id(a)),
            "name": _account_label(a),
            "variant": a.get("variant", "ai"),
        }
        for a in accounts
        if _account_id(a) and (not wanted or str(_account_id(a)) in wanted)
    ]
    return {
        "ok": True,
        "config": cfg,
        "accounts": [
            {"id": str(_account_id(a)), "name": _account_label(a), "variant": a.get("variant", "ai")}
            for a in accounts if _account_id(a)
        ],
        "activeAccounts": len(selected),
        "intervalMinutes": cfg.get("intervalMinutes"),
        "minIntervalMinutes": model_health.MIN_INTERVAL_MINUTES,
        # 让界面能显示「手动禁用 N 项 / 巡检禁用 M 项」：
        # 两者含义不同（人为决定 vs 自动探测），恢复方式也不同。
        "disabledBySource": model_policy.count_by_source(),
        "autoEnableProtectsManual": True,
        "runtime": dict(_HEALTH_RUNTIME),
    }


@app.get("/model-health/config")
def model_health_config():
    """返回巡检配置、可选账号清单与上次运行状态。"""
    return health_config_snapshot()


@app.put("/model-health/config")
def update_model_health_config(payload: Dict[str, Any]):
    """更新巡检配置（只覆盖显式给出的字段）。"""
    model_health.merge_config(payload or {})
    return health_config_snapshot()


@app.get("/model-health/status")
def model_health_status():
    return {
        "ok": True,
        "config": model_health.load_config(),
        "runtime": dict(_HEALTH_RUNTIME),
        "disabledTotal": model_policy.disabled_total(),
        "disabledBySource": model_policy.count_by_source(),
    }


@app.post("/model-health/run")
def model_health_run(payload: Optional[Dict[str, Any]] = None):
    """立即执行一轮巡检（不改变开关状态）。

    请求体可临时覆盖本轮参数（如只探测某几个账号），便于先小范围试跑。
    """
    overrides = payload or {}
    result = run_model_health_check(overrides=overrides if isinstance(overrides, dict) else None)
    return {"ok": True, "result": result}


def run_model_health_check(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行一轮巡检并记录运行状态。任何异常都不向上抛，只记录到 lastError。

    并发调用会被直接拒绝：手动「立即巡检」与后台定时轮次可能同时到达，
    没有互斥就会双倍消耗上游额度，还会让两轮各自读到的策略互相覆盖。
    """
    if not _HEALTH_RUN_LOCK.acquire(blocking=False):
        return {"error": "已有巡检正在进行，请稍后再试", "skipped": True}
    try:
        cfg = model_health.load_config()
        if overrides:
            merged = dict(cfg)
            for key in ("accountIds", "onlyUsedModels", "autoEnable"):
                if key in overrides:
                    merged[key] = overrides[key]
            cfg = model_health._coerce_config(merged)

        _HEALTH_RUNTIME["running"] = True
        try:
            raw = model_health.run_round(
                accounts=_load_accounts(),
                config=cfg,
                client_factory=lambda: httpx.Client(timeout=30.0),
                base_url_for=_health_base_url,
                used_models=_used_models_by_account(),
                base_models=_base_model_ids(),
            )
            summary = model_health.summarize(raw)
            # 逐账号明细只在这种「手动触发」的响应里返回，便于排查；
            # 定时轮次不返回（没人看，只是白白撑大内存里的状态）。
            summary["reports"] = raw.get("reports", [])
            _HEALTH_RUNTIME["lastRunAt"] = summary.get("checkedAt")
            _HEALTH_RUNTIME["lastResult"] = model_health.summarize(raw)
            # 出问题时要让界面把它当异常显示出来，而不是伪装成一次
            # 「什么都没变」的正常巡检：整轮作废、探测被上游拒绝、账号凭据失效
            # 都属于「本轮结果不可信」，逐个给出原因。
            _HEALTH_RUNTIME["lastError"] = _health_problem_note(summary)
            print(f"[health] round done: combos={summary.get('combos')} "
                  f"available={summary['counts'].get('available')} "
                  f"unavailable={summary['counts'].get('unavailable')} "
                  f"transient={summary['counts'].get('transient')} "
                  f"probeDefect={summary['counts'].get('probe_defect')} "
                  f"authFailed={len(summary.get('authFailed') or [])} "
                  f"aborted={summary.get('aborted')}", flush=True)
            return summary
        except Exception as e:
            _HEALTH_RUNTIME["lastError"] = f"{type(e).__name__}: {e}"
            print(f"[health] run failed: {e}", flush=True)
            return {"error": _HEALTH_RUNTIME["lastError"]}
        finally:
            _HEALTH_RUNTIME["running"] = False
    finally:
        _HEALTH_RUN_LOCK.release()


async def model_health_loop() -> None:
    """按配置间隔执行巡检。配置改成关闭时，下一轮自动跳过（不退出循环）。

    巡检本身是同步阻塞的（几十上百次 HTTP 往返），必须丢到线程里跑，
    否则会把事件循环卡死 —— 巡检期间所有转发请求都会一起停摆。
    """
    print("[health] model availability check loop started")
    while True:
        try:
            cfg = model_health.load_config()
            interval = int(cfg.get("intervalMinutes") or model_health.DEFAULT_INTERVAL_MINUTES)
            if cfg.get("enabled"):
                # 结果日志由 run_model_health_check 统一打印（手动 / 定时共用一条口径）
                await asyncio.to_thread(run_model_health_check)
            # 关闭状态也要按间隔轮询配置变更，否则改完开关要等整整一个周期才生效。
            await asyncio.sleep(interval * 60)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[health] loop error: {e}", flush=True)
            await asyncio.sleep(60)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("API_PORT", 18091))
    uvicorn.run(app, host="0.0.0.0", port=port)
