"""模型可用性自动巡检。

背景：账号池解决「哪个账号参与调用」，模型级禁用解决「这个账号的这个模型不参与调用」。
但禁用此前只能靠人手动点 —— 某个账号的某个模型因为优惠政策调整、免费额度到期或上游限流
调不通时，得等人发现、再手动去点一下。

本模块把这件事自动化：按设定间隔，逐个探测「账号 × 模型」组合是否真的可用，
不可用就自动写进禁用策略，恢复可用就自动移除，回到可用状态。

设计要点
--------
1. **复用模型级禁用策略作为唯一事实来源**。自动巡检不另建一套状态，
   它读写的就是 ``model_policy.json`` —— 所以「自动禁用」和「手动禁用」是同一件事，
   手动关掉巡检后，之前自动写进去的禁用项依然生效，可继续手动增删。
2. **探测量小**。每个组合发一个极小的非流式请求（1 token、短提示词），
   只看「有没有正常返回」而不看内容质量。
3. **不做全量遍历的默认值**。账号 × 模型可能上百个组合，默认探测范围收窄到
   「该账号曾经调用过的模型 + 内置基础清单」，避免一上来就把上游打爆。
4. **失败不怕**。探测网络异常、超时、上游 5xx 一律计为「不可用」并记原因；
   写盘失败、加载失败一律吞掉，绝不影响正常 API 请求。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    from gateway import model_policy
except ImportError:
    import model_policy


# 巡检配置落盘位置（与 model_policy.json、account_pool_config.json 并列，互不覆盖）
DATA_DIR = Path(os.getenv("WB_DATA_DIR", "/data/.wb-switch"))
HEALTH_CONFIG_FILE = DATA_DIR / "model_health_config.json"

# 判定「不可用」的 HTTP 状态码：这些明确表示这个组合现在调不通。
# 429（限流）是**临时**状态，不计入自动禁用 —— 否则高峰期巡检会把好模型全禁掉。
UNAVAILABLE_STATUS = {400, 401, 403, 404, 422, 500, 502, 503, 504}
TRANSIENT_STATUS = {408, 425, 429}

DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 5

# 探测用的最小请求体：只要求回一个字，把计费与耗时压到最低。
PROBE_MESSAGES = [{"role": "user", "content": "hi"}]
PROBE_MAX_TOKENS = 1

_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------

def default_config() -> Dict[str, Any]:
    """默认关闭。自动禁用是个会改行为的动作，不能默认替用户打开。"""
    return {
        "enabled": False,
        "intervalMinutes": DEFAULT_INTERVAL_MINUTES,
        # 空数组 = 全部账号参与巡检（与账号池 enabledAccountIds 的语义保持一致）
        "accountIds": [],
        # 是否只探测「该账号调用过的模型」。开启可大幅减少探测次数。
        "onlyUsedModels": True,
        # 自动启用：探测通过时，是否自动移除该组合的禁用项。
        "autoEnable": True,
        # 巡检结束后是否写流水
        "logResults": True,
    }


def _coerce_config(raw: Any) -> Dict[str, Any]:
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg

    if "enabled" in raw:
        cfg["enabled"] = bool(raw.get("enabled"))
    if "autoEnable" in raw:
        cfg["autoEnable"] = bool(raw.get("autoEnable"))
    if "onlyUsedModels" in raw:
        cfg["onlyUsedModels"] = bool(raw.get("onlyUsedModels"))
    if "logResults" in raw:
        cfg["logResults"] = bool(raw.get("logResults"))

    try:
        interval = int(raw.get("intervalMinutes", DEFAULT_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_MINUTES
    cfg["intervalMinutes"] = max(MIN_INTERVAL_MINUTES, interval)

    account_ids = raw.get("accountIds")
    if isinstance(account_ids, list):
        cfg["accountIds"] = sorted({str(a) for a in account_ids if a})
    return cfg


def load_config() -> Dict[str, Any]:
    """读取配置；文件缺失 / 损坏 / 结构不对一律回退默认值，绝不抛异常。"""
    try:
        if HEALTH_CONFIG_FILE.exists():
            with open(HEALTH_CONFIG_FILE, "r", encoding="utf-8") as f:
                return _coerce_config(json.load(f))
    except Exception as e:
        print(f"[health] failed to load config: {e}", flush=True)
    return default_config()


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """整份重写 + 原子替换。写盘失败一律吞掉，绝不能把 API 请求打死。"""
    cfg = _coerce_config(cfg)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_CONFIG_FILE.with_name(HEALTH_CONFIG_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, HEALTH_CONFIG_FILE)
    except Exception as e:
        print(f"[health] failed to persist config: {e}", flush=True)
    return cfg


def merge_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把请求里的字段并入现有配置（只覆盖显式给出的键）。"""
    current = load_config()
    if isinstance(payload, dict):
        for key in ("enabled", "intervalMinutes", "accountIds",
                    "onlyUsedModels", "autoEnable", "logResults"):
            if key in payload:
                current[key] = payload[key]
    return save_config(current)


# ---------------------------------------------------------------------------
# 巡检账号 / 模型范围
# ---------------------------------------------------------------------------

def select_targets(accounts: List[Dict[str, Any]], config: Dict[str, Any],
                   used_models: Optional[Dict[str, Set[str]]] = None,
                   base_models: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """算出本次要探测的「账号 × 模型」组合清单。

    ``accountIds`` 为空数组 = 全部账号参与（与账号池语义一致）。
    只保留 token 未过期的账号 —— 过期的账号探测必然失败，探测它没有意义。
    """
    now_ms = int(time.time() * 1000)
    wanted = set(config.get("accountIds") or [])

    usable: List[Dict[str, Any]] = []
    for acc in accounts:
        acc_id = acc.get("id") or acc.get("uid")
        if not acc_id:
            continue
        if wanted and str(acc_id) not in wanted:
            continue
        if not acc.get("access_token"):
            continue
        expires_at = acc.get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at > 0 and expires_at <= now_ms:
            continue
        usable.append(acc)

    fallback = list(base_models or [])
    only_used = bool(config.get("onlyUsedModels", True))

    combos: List[Dict[str, str]] = []
    for acc in usable:
        acc_id = str(acc.get("id") or acc.get("uid"))
        if only_used and used_models is not None:
            models = sorted(used_models.get(acc_id) or [])
            # 该账号还没有任何调用记录时，退回基础清单，否则新账号永远不被巡检。
            if not models:
                models = fallback
        else:
            models = fallback
        for model in models:
            if model:
                combos.append({"accountId": acc_id, "model": str(model)})
    return combos


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------

def classify_probe(status_code: Optional[int], error: Optional[str] = None) -> str:
    """把一次探测结果归类为 unavailable / transient / available。

    - 明确的功能性失败（模型不存在、无权限、上游 5xx）→ ``unavailable``
    - 限流 / 超时等**临时**状态 → ``transient``，不参与自动禁用
    - 正常返回 → ``available``
    """
    if error:
        return "transient"
    if status_code is None:
        return "transient"
    if 200 <= status_code < 300:
        return "available"
    if status_code in TRANSIENT_STATUS:
        return "transient"
    if status_code in UNAVAILABLE_STATUS:
        return "unavailable"
    return "transient"


def probe_once(client: Any, base_url: str, token: str, model: str,
               timeout: float = 30.0) -> Dict[str, Any]:
    """同步探测一个「账号 × 模型」组合。返回探测结果字典。

    只看 HTTP 状态，不解析响应体 —— 探测的目的是「这条路通不通」，
    不是「回答得好不好」，所以不记录模型输出，避免把探测内容混进用量统计。
    """
    started = time.time()
    payload = {
        "model": model,
        "messages": PROBE_MESSAGES,
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status_code: Optional[int] = None
    error: Optional[str] = None
    try:
        res = client.post(f"{base_url}/chat/completions", json=payload, headers=headers,
                          timeout=timeout)
        status_code = int(getattr(res, "status_code", 0) or 0)
    except Exception as e:  # 网络异常 / 超时 / DNS 失败
        error = f"{type(e).__name__}: {e}"

    verdict = classify_probe(status_code, error)
    return {
        "model": model,
        "status": status_code,
        "verdict": verdict,          # available / unavailable / transient
        "error": error,
        "elapsedMs": int((time.time() - started) * 1000),
    }


# ---------------------------------------------------------------------------
# 结果落地到禁用策略
# ---------------------------------------------------------------------------

def apply_verdicts(policy: Dict[str, List[str]], account_id: str, results: List[Dict[str, Any]],
                   auto_enable: bool = True) -> Dict[str, Any]:
    """把一轮探测结果写回策略。

    - ``unavailable`` → 加入禁用列表（自动禁用）
    - ``available`` 且 ``auto_enable`` → 从禁用列表移除（自动启用）
    - ``transient`` → **不动**，保持原状

    返回变更明细，供界面展示与排查。
    """
    acc_key = str(account_id)
    current = set(policy.get(acc_key) or [])
    before = set(current)

    disabled_now: List[str] = []
    enabled_now: List[str] = []
    unchanged: List[str] = []

    for item in results:
        model = str(item.get("model") or "")
        if not model:
            continue
        verdict = item.get("verdict")
        if verdict == "unavailable":
            if model not in current:
                current.add(model)
                disabled_now.append(model)
            else:
                unchanged.append(model)
        elif verdict == "available":
            if model in current:
                if auto_enable:
                    current.discard(model)
                    enabled_now.append(model)
                else:
                    unchanged.append(model)
            else:
                unchanged.append(model)
        else:
            unchanged.append(model)

    if current:
        policy[acc_key] = sorted(current)
    else:
        # 该账号已无任何禁用模型，删掉键让配置文件保持干净。
        policy.pop(acc_key, None)

    return {
        "accountId": acc_key,
        "disabled": sorted(disabled_now),
        "enabled": sorted(enabled_now),
        "unchanged": sorted(unchanged),
        "disabledBefore": sorted(before),
        "disabledAfter": sorted(current),
        "changed": sorted(current) != sorted(before),
    }


# ---------------------------------------------------------------------------
# 巡检轮次（把「算组合 → 探测 → 写策略」串起来）
# ---------------------------------------------------------------------------

def run_round(accounts: List[Dict[str, Any]],
              config: Dict[str, Any],
              client_factory: Any,
              base_url_for: Any,
              used_models: Optional[Dict[str, Set[str]]] = None,
              base_models: Optional[List[str]] = None,
              now_ms: Optional[int] = None) -> Dict[str, Any]:
    """执行一轮完整巡检并落盘。返回汇总结果。

    ``client_factory`` 与 ``base_url_for`` 由调用方注入，便于单测替换成假客户端。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    combos = select_targets(accounts, config, used_models, base_models)
    by_id = {}
    for acc in accounts:
        acc_id = acc.get("id") or acc.get("uid")
        if acc_id:
            by_id[str(acc_id)] = acc

    policy = model_policy.load_policy()
    account_reports: List[Dict[str, Any]] = []
    counts = {"available": 0, "unavailable": 0, "transient": 0}

    # 按账号分组探测，这样一个账号只建一次 HTTP 连接。
    grouped: Dict[str, List[str]] = {}
    for combo in combos:
        grouped.setdefault(combo["accountId"], []).append(combo["model"])

    client = client_factory()
    try:
        for acc_id, models in grouped.items():
            acc = by_id.get(acc_id)
            if not acc:
                continue
            token = acc.get("access_token")
            variant = acc.get("variant", "ai")
            base_url = base_url_for(variant)

            results = []
            for model in models:
                item = probe_once(client, base_url, token, model)
                counts[item["verdict"]] += 1
                results.append(item)

            report = apply_verdicts(policy, acc_id, results,
                                    auto_enable=bool(config.get("autoEnable", True)))
            report["accountName"] = acc.get("nickname") or acc.get("email") or acc_id
            report["results"] = results
            account_reports.append(report)
    finally:
        try:
            client.close()
        except Exception:
            pass

    model_policy.save_policy(policy)

    return {
        "checkedAt": now_ms,
        "combos": len(combos),
        "accounts": len(grouped),
        "counts": counts,
        "disabled": sorted({m for r in account_reports for m in r["disabled"]}),
        "enabled": sorted({m for r in account_reports for m in r["enabled"]}),
        "reports": account_reports,
    }


def summarize(round_result: Dict[str, Any]) -> Dict[str, Any]:
    """给界面用的精简摘要（不带逐条探测明细，避免响应体过大）。"""
    return {
        "checkedAt": round_result.get("checkedAt"),
        "combos": round_result.get("combos", 0),
        "accounts": round_result.get("accounts", 0),
        "counts": round_result.get("counts", {}),
        "disabled": round_result.get("disabled", []),
        "enabled": round_result.get("enabled", []),
    }
