"""模型可用性自动巡检。

背景：账号池解决「哪个账号参与调用」，模型级禁用解决「这个账号的这个模型不参与调用」。
但禁用此前只能靠人手动点 —— 某个账号的某个模型因为优惠政策调整、免费额度到期或上游限流
调不通时，得等人发现、再手动去点一下。

本模块把这件事自动化：按设定间隔，逐个探测「账号 × 模型」组合是否真的可用，
不可用就自动写进禁用策略，恢复可用就自动移除，回到可用状态。

设计要点
--------
1. **复用模型级禁用策略作为唯一事实来源**。自动巡检不另建一套状态，
   它读写的就是 ``model_policy.json``。每个禁用项带来源标记（manual / auto），
   于是「自愈」只放开自己写进去的那些，人手点的一律不动 —— 详见 model_policy 模块。
2. **探测必须模仿真实调用的请求形态**。上游只接受流式请求（非流式直接 400）、
   要求首条消息是 system prompt、并且对 ``max_tokens`` 有最小值校验。
   探测请求体因此刻意做成「什么都不多带」的最小合法请求，与网关转发上游时保持一致，
   否则会得到「全部模型都不可用」这种荒谬结论，进而成批误禁好模型。
3. **探测量小靠早停，不靠 ``max_tokens``**。读完流式响应的第一帧就断开连接，
   耗时与计费都压到最低，同时不给上游任何可校验的参数。
4. **宁漏禁，不误禁**。限流 / 超时等临时状态一律跳过；请求被上游参数校验拒绝时
   判定为探测自身的问题（``probe_defect``）同样跳过；整轮无一个可用时整轮作废、
   不写策略。凭据整账号失效时也不写 —— 坏的是凭据，不是模型。
5. **失败不怕**。探测网络异常一律记为临时状态；写盘失败、加载失败一律吞掉，
   绝不影响正常 API 请求。
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from gateway import model_policy
except ImportError:
    import model_policy


# 巡检配置落盘位置（与 model_policy.json、account_pool_config.json 并列，互不覆盖）
DATA_DIR = Path(os.getenv("WB_DATA_DIR", "/data/.wb-switch"))
HEALTH_CONFIG_FILE = DATA_DIR / "model_health_config.json"
# 最近一轮结果的落盘位置。只保存摘要，用于「重启后界面仍能看到上次巡检」。
LAST_RUN_FILE = DATA_DIR / "model_health_last.json"

# 判定「不可用」的 HTTP 状态码：这些明确表示这个组合现在调不通。
# 429（限流）是**临时**状态，不计入自动禁用 —— 否则高峰期巡检会把好模型全禁掉。
UNAVAILABLE_STATUS = {400, 401, 403, 404, 422, 500, 502, 503, 504}
TRANSIENT_STATUS = {408, 425, 429}
# 凭据失效。一个账号的 token 过期会让它名下**所有**模型一起失败，
# 但坏的是凭据不是模型，因此单独识别、不写禁用策略。
AUTH_STATUS = {401, 403}
# 整个账号都栽在鉴权上、且至少探了这么多个模型时，按「凭据失效」处理。
# 取 2 是为了避免只探一个组合就下结论。
AUTH_GUARD_MIN_MODELS = 2

# 上游表示「请求参数被模型提供方拒绝」的错误码：问题出在**探测请求本身**，
# 与模型可用性无关。命中时归为 probe_defect，只跳过、不写策略。
PROBE_DEFECT_CODES = {11133}
# 响应体里出现这些片段，同样判定为探测请求自身的形态问题。
# 典型来源：给上游传了超出取值范围的可选参数（如 max_tokens 低于最小值）。
PROBE_DEFECT_HINTS = (
    "integer_below_min_value",
    "integer_above_max_value",
    "invalid_parameter",
    "string_too_long",
    "string_too_short",
)

DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 5

# 探测用的最小请求体。请求体里**只放必需的字段**：任何可选的、有取值范围校验的
# 字段（比如 max_tokens）都可能被上游以参数错误拒掉，进而伪装成「模型不可用」。
PROBE_USER_CONTENT = "hi"
# 上游要求首条消息必须是 system prompt，否则回 400
# （`first message is not system prompt`）。这里与网关的
# `normalize_messages_for_upstream()` 保持同一语义，两处改动需同步。
PROBE_SYSTEM_PROMPT = "You are a helpful assistant."

# 一整轮里「可用」为 0 且「不可用 + 探测被拒」达到这个数量时，判定为探测机制本身
# 出了问题（鉴权失效、请求体形态被上游拒绝、上游整体故障），整轮作废不写策略。
# 宁可漏禁，也不能把一批好模型成批误禁 —— 后者要靠人工逐个放开，代价高得多。
GUARD_MIN_UNAVAILABLE = 5

# 巡检落盘前留底的备份后缀，即 ``model_policy.json.before-health-check``。
# 出问题时把这份备份恢复回去、重启容器，即可整份回到巡检改动之前。
BACKUP_SUFFIX = "before-health-check"

# 从错误响应体里抠出上游错误码，用于诊断与分类。
_ERROR_CODE_RE = re.compile(r'"code"\s*:\s*"?(\d+)"?')
# 只留错误响应体的前若干字符做判定，避免把大段响应读进内存。
_SNIPPET_LIMIT = 400


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
        # 自动启用：探测通过时，是否自动移除禁用项。只会移除巡检自己写入的项，
        # 手动禁用的模型受来源标记保护，不受此开关影响。
        "autoEnable": True,
        # 是否把每轮结果落盘，供重启后界面回显
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


def save_last_run(summary: Dict[str, Any]) -> None:
    """把最近一轮摘要落盘，重启后仍能回显。失败不抛异常。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LAST_RUN_FILE.with_name(LAST_RUN_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LAST_RUN_FILE)
    except Exception as e:
        print(f"[health] failed to persist last run: {e}", flush=True)


def load_last_run() -> Optional[Dict[str, Any]]:
    try:
        if LAST_RUN_FILE.exists():
            with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
                value = json.load(f)
            return value if isinstance(value, dict) else None
    except Exception:
        pass
    return None


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

def build_probe_body(model: str) -> Dict[str, Any]:
    """构造探测请求体。**必须与网关转发上游时的形态一致**，否则结果不可信：

    - ``stream: True`` —— 上游不接受非流式请求（回 400
      ``Non-stream chat request is currently not supported``）；
    - 首条消息为 system —— 否则回 400 ``first message is not system prompt``；
    - **不带 ``max_tokens``** —— 上游对它有最小值校验，且最小值随模型系列变化。
      为了省一点输出而传一个很小的值，会被回 400 ``integer_below_min_value``，
      表现为「这批模型全部不可用」，代价远高于省下的那点额度。
      不传该字段时上游按默认上限处理，与普通客户端请求完全一致。
      探测的「量小」由 ``probe_once`` 读完第一帧就断开连接来保证。
    """
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": PROBE_SYSTEM_PROMPT},
            {"role": "user", "content": PROBE_USER_CONTENT},
        ],
        "stream": True,
    }


def _extract_error_code(snippet: Optional[str]) -> Optional[int]:
    """从上游错误响应体里取错误码；取不到就返回 None。"""
    if not snippet:
        return None
    match = _ERROR_CODE_RE.search(snippet)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _looks_like_probe_defect(code: Optional[int], snippet: Optional[str]) -> bool:
    """判断这次失败是不是「探测请求自己有问题」而不是「模型不可用」。"""
    if code is not None and code in PROBE_DEFECT_CODES:
        return True
    text = snippet or ""
    return any(hint in text for hint in PROBE_DEFECT_HINTS)


def classify_probe(status_code: Optional[int], error: Optional[str] = None,
                   code: Optional[int] = None,
                   snippet: Optional[str] = None) -> str:
    """把一次探测结果归类为 available / unavailable / transient / probe_defect。

    - 正常返回 → ``available``
    - 上游拒绝了我们的请求参数 → ``probe_defect``，说明探测形态不对，
      **不能**据此判定模型好坏，不参与自动禁用
    - 限流 / 超时等**临时**状态 → ``transient``，不参与自动禁用
    - 明确的功能性失败（模型不存在、无权限、上游 5xx）→ ``unavailable``
    """
    if error:
        return "transient"
    if status_code is None:
        return "transient"
    if 200 <= status_code < 300:
        return "available"
    # 先于状态码判断：400 既可能是「模型不可用」，也可能是「请求参数不对」，
    # 后者绝不能当成模型的问题 —— 否则一次参数改动就能成批误禁好模型。
    if _looks_like_probe_defect(code, snippet):
        return "probe_defect"
    if status_code in TRANSIENT_STATUS:
        return "transient"
    if status_code in UNAVAILABLE_STATUS:
        return "unavailable"
    return "transient"


def _send_probe(client: Any, url: str, body: Dict[str, Any], headers: Dict[str, str],
                timeout: float) -> Tuple[Optional[int], str]:
    """发一次探测请求，返回 ``(状态码, 响应体片段)``。

    优先走流式读取：拿到第一帧就断开连接。上游只接受 ``stream: True``，
    而完整读完一次生成会白白消耗额度，首帧已足以判断这条链路通不通。
    客户端没有 ``stream()``（单测里的假客户端）时退回普通 ``post()``。
    """
    stream = getattr(client, "stream", None)
    if callable(stream):
        with stream("POST", url, json=body, headers=headers, timeout=timeout) as res:
            status = int(getattr(res, "status_code", 0) or 0)
            snippet = ""
            if 200 <= status < 300:
                # 只取第一帧：SSE 每帧以换行分隔，取一行即可确认确实在流式返回。
                try:
                    for line in res.iter_lines():
                        snippet = line if isinstance(line, str) else line.decode("utf-8", "replace")
                        break
                except Exception:
                    snippet = ""
            else:
                # 错误响应体很小，整份读出来才能看清上游给的原因。
                try:
                    raw = res.read()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    snippet = raw or ""
                except Exception:
                    snippet = ""
            return status, snippet[:_SNIPPET_LIMIT]

    res = client.post(url, json=body, headers=headers, timeout=timeout)
    status = int(getattr(res, "status_code", 0) or 0)
    text = getattr(res, "text", "") or ""
    return status, str(text)[:_SNIPPET_LIMIT]


def probe_once(client: Any, base_url: str, token: str, model: str,
               timeout: float = 30.0) -> Dict[str, Any]:
    """同步探测一个「账号 × 模型」组合。返回探测结果字典。

    只看状态码，不解析模型输出 —— 探测的目的是「这条路通不通」，
    不是「回答得好不好」，所以不记录输出内容，避免把探测混进用量统计。
    请求体刻意不带 ``max_tokens``（见 ``build_probe_body``），
    靠读完第一帧就断开来压低耗时与计费。
    """
    started = time.time()
    body = build_probe_body(model)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status_code: Optional[int] = None
    error: Optional[str] = None
    snippet = ""
    try:
        status_code, snippet = _send_probe(client, f"{base_url}/chat/completions",
                                           body, headers, timeout)
    except Exception as e:  # 网络异常 / 超时 / DNS 失败
        error = f"{type(e).__name__}: {e}"

    code = _extract_error_code(snippet)
    verdict = classify_probe(status_code, error, code, snippet)
    return {
        "model": model,
        "status": status_code,
        "verdict": verdict,          # available / unavailable / transient / probe_defect
        "code": code,                # 上游错误码，仅用于诊断
        "error": error,
        "elapsedMs": int((time.time() - started) * 1000),
    }


# ---------------------------------------------------------------------------
# 结果落地到禁用策略
# ---------------------------------------------------------------------------

def apply_verdicts(policy: Dict[str, Any], account_id: str, results: List[Dict[str, Any]],
                   auto_enable: bool = True) -> Dict[str, Any]:
    """把一轮探测结果写回策略。

    - ``unavailable`` 且当前未禁用 → 标记为 ``auto`` 禁用
    - ``unavailable`` 且已被禁用 → 保持原来源不变（手动禁用不会被改写为 auto）
    - ``available`` 且 ``auto_enable`` 且来源是 ``auto`` → 移除（自愈）
    - ``available`` 但来源是 ``manual`` → **不动**，手动禁用受保护
    - ``transient`` / ``probe_defect`` → **不动**（探测没得到有效结论）

    返回变更明细，供界面展示与排查。
    """
    acc_key = str(account_id)
    entry = dict(model_policy._coerce_entry(policy.get(acc_key)) or {})
    before = set(entry)

    disabled_now: List[str] = []
    enabled_now: List[str] = []
    protected: List[str] = []      # 因「手动禁用」而保持原样的项
    unchanged: List[str] = []

    for item in results:
        model = str(item.get("model") or "")
        if not model:
            continue
        verdict = item.get("verdict")
        source = entry.get(model)

        if verdict == "unavailable":
            if source is None:
                entry[model] = model_policy.SOURCE_AUTO
                disabled_now.append(model)
            else:
                # 已禁用（人手点的或上次巡检写的）：不覆盖来源标记，
                # 免得一次探测把手动禁用「降级」成可被自愈放开的 auto 项。
                if source == model_policy.SOURCE_MANUAL:
                    protected.append(model)
                unchanged.append(model)
        elif verdict == "available":
            if source is None:
                unchanged.append(model)
            elif source == model_policy.SOURCE_MANUAL:
                protected.append(model)
                unchanged.append(model)
            elif auto_enable:
                entry.pop(model, None)
                enabled_now.append(model)
            else:
                unchanged.append(model)
        else:
            # transient / probe_defect：没拿到有效结论，一律保持原样。
            unchanged.append(model)

    if entry:
        policy[acc_key] = entry
    else:
        # 该账号已无任何禁用模型，删掉键让配置文件保持干净。
        policy.pop(acc_key, None)

    return {
        "accountId": acc_key,
        "disabled": sorted(disabled_now),
        "enabled": sorted(enabled_now),
        "protected": sorted(protected),
        "unchanged": sorted(unchanged),
        "disabledBefore": sorted(before),
        "disabledAfter": sorted(entry),
        "changed": set(entry) != before,
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

    写入策略前有三道闸门，全部服务于「宁漏禁，不误禁」：
    探测被上游参数校验拒绝（``probe_defect``）不写；账号凭据整体失效不写；
    整轮一个可用都没有时整轮作废。
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
    counts = {"available": 0, "unavailable": 0, "transient": 0, "probe_defect": 0}
    auth_failed: List[str] = []
    defect_codes: Set[int] = set()

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
                counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
                if item.get("verdict") == "probe_defect" and item.get("code") is not None:
                    defect_codes.add(int(item["code"]))
                results.append(item)

            existing = sorted(model_policy._coerce_entry(policy.get(acc_id)) or {})
            statuses = {r.get("status") for r in results}
            # 整账号凭据失效：token 过期会让名下**所有**模型一起变红，
            # 但坏的是凭据而不是模型。这种情况不写禁用策略，只提示重新登录 ——
            # 否则一次 token 过期就会自动禁掉整个账号的模型清单。
            if (len(results) >= AUTH_GUARD_MIN_MODELS
                    and statuses and statuses <= AUTH_STATUS):
                auth_failed.append(acc_id)
                report = {
                    "accountId": acc_id,
                    "disabled": [],
                    "enabled": [],
                    "protected": [],
                    "unchanged": sorted(r["model"] for r in results),
                    "disabledBefore": existing,
                    "disabledAfter": existing,
                    "changed": False,
                    "authFailed": True,
                }
            else:
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

    # 整轮保护：一个可用的都没有，而且失败集中在「探测机制本身有问题」的两类上
    # （请求被上游参数校验拒绝、网络与限流），几乎可以肯定是探测侧失效
    # （凭据、请求形态、上游整体故障），而不是所有模型同时坏掉。
    # 此时不写策略 —— 成批误禁要靠人工逐个放开，代价远高于漏禁一轮。
    no_signal = counts["unavailable"] + counts["probe_defect"]
    aborted = counts["available"] == 0 and no_signal >= GUARD_MIN_UNAVAILABLE
    if aborted:
        # 逐账号明细里的变更声明要一并清空：策略没有落盘，
        # 留着会让人以为「这些已经被禁了」，而实际什么都没写。
        for report in account_reports:
            report["disabled"] = []
            report["enabled"] = []
            report["changed"] = False
        if counts["probe_defect"] >= GUARD_MIN_UNAVAILABLE:
            codes = "、".join(str(c) for c in sorted(defect_codes)) or "未知"
            reason = (
                f"本轮 {counts['probe_defect']} 个组合的探测请求被上游参数校验拒绝"
                f"（错误码 {codes}），说明探测请求的形态与真实调用不一致，"
                "已跳过写入。请升级镜像或反馈该错误码。"
            )
        else:
            reason = (
                f"本轮 {counts['unavailable']} 个组合全部不可用、无一个可用，"
                "判定为探测异常而非模型失效，已跳过写入。请检查账号凭据与上游状态。"
            )
    else:
        reason = None
        # 有实际改动才留底：一轮巡检可能同时改动上百个条目，
        # 判定万一有误，用户需要的是整份回退，而不是逐个点回来。
        # 无改动时不覆盖，好让更早的那份底尽可能久地留着。
        if any(r["disabled"] or r["enabled"] for r in account_reports):
            model_policy.backup_policy(BACKUP_SUFFIX)
        model_policy.save_policy(policy)

    result = {
        "checkedAt": now_ms,
        "combos": len(combos),
        "accounts": len(grouped),
        "counts": counts,
        "disabled": sorted({m for r in account_reports for m in r["disabled"]}),
        "enabled": sorted({m for r in account_reports for m in r["enabled"]}),
        "protected": sorted({m for r in account_reports for m in r["protected"]}),
        # 凭据失效被跳过的账号：界面据此提示「请重新登录」，
        # 而不是让人对着「一轮下来什么都没变」的结果猜原因。
        "authFailed": [
            {"id": r["accountId"], "name": r.get("accountName")}
            for r in account_reports if r.get("authFailed")
        ],
        # 探测被上游参数校验拒绝时命中的错误码，用于排查探测形态问题。
        "defectCodes": sorted(defect_codes),
        "reports": account_reports,
        "aborted": aborted,
        "reason": reason,
    }

    if config.get("logResults", True):
        save_last_run(summarize(result))
    return result


def summarize(round_result: Dict[str, Any]) -> Dict[str, Any]:
    """给界面用的精简摘要（不带逐条探测明细，避免响应体过大）。"""
    return {
        "checkedAt": round_result.get("checkedAt"),
        "combos": round_result.get("combos", 0),
        "accounts": round_result.get("accounts", 0),
        "counts": round_result.get("counts", {}),
        "disabled": round_result.get("disabled", []),
        "enabled": round_result.get("enabled", []),
        "protected": round_result.get("protected", []),
        "authFailed": round_result.get("authFailed", []),
        "defectCodes": round_result.get("defectCodes", []),
        "aborted": bool(round_result.get("aborted")),
        "reason": round_result.get("reason"),
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
        "protected": round_result.get("protected", []),
        "aborted": bool(round_result.get("aborted")),
        "reason": round_result.get("reason"),
    }
