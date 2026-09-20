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

try:
    from gateway import account_policy
except ImportError:
    import account_policy


# 巡检配置落盘位置（与 model_policy.json、account_pool_config.json 并列，互不覆盖）
DATA_DIR = Path(os.getenv("WB_DATA_DIR", "/data/.wb-switch"))
HEALTH_CONFIG_FILE = DATA_DIR / "model_health_config.json"
# 最近一轮结果的落盘位置。只保存摘要，用于「重启后界面仍能看到上次巡检」。
LAST_RUN_FILE = DATA_DIR / "model_health_last.json"

# 判定「不可用」的 HTTP 状态码：这些明确表示这个组合现在调不通。
# 429（限流）是**临时**状态，不计入自动禁用 —— 否则高峰期巡检会把好模型全禁掉。
UNAVAILABLE_STATUS = {400, 401, 403, 404, 422, 500, 502, 503, 504}
TRANSIENT_STATUS = {408, 425, 429}
# 凭据失效**只认** 401。403 曾被一并算作失效，那是错的：
# 它同样可能是内容安全审查（错误码 11140），而那种情况下凭据是好的。
# 精确判定请走 _CODE_SEMANTICS 语义表 —— 这张表只作为语义识别不出时的兜底。
AUTH_STATUS = {401}

# ---------------------------------------------------------------------------
# 上游错误码 → 语义类别（唯一事实来源）
# ---------------------------------------------------------------------------
#
# 为什么要有这张表：判定曾经散落在多处 `status in {…}` 集合里，同一份响应在
# 不同路径上得到不同结论 —— 同一批账号，「检测账号」说凭据有效、巡检说凭据失效。
# 根因是 HTTP 状态码**不足以**表达语义：403 既可能是 token 过期，也可能是内容
# 被安全审查拦下（凭据其实好好的）。必须看上游错误码才能分清。
#
# 因此：所有判定一律先查这张表；只有表里查不到时，才退回按状态码粗判。
# 新增一处判定时不要再写 status 集合，往这里加一行。
#
# 类别语义：
#   auth        —— 凭据本身失效（token 过期 / 被吊销）。只此一类能判「需要重新登录」。
#   restricted  —— 账号被上游策略拦截（内容安全审查、账号受限）。凭据有效，
#                  但该账号当前发不出请求。既不该提示重新登录，也不该禁用模型。
#   quota       —— 该账号对这个模型无权限 / 额度用尽。账号整体没问题，只影响这个模型。
#   model_missing —— 模型不存在。对**凭据探测**来说是「认下了 token」的证据。
#   probe_defect —— 请求形态本身不被接受（非流式、参数越界）。是探测的错，不是账号的错。
#   invalid_model_name —— 模型名格式不合法（如含非法字符），同样属探测形态问题。
_CODE_SEMANTICS: Dict[int, str] = {
    # —— 凭据 ——
    11100: "auth",            # token 无效 / 已过期
    11102: "model_missing",   # model service info not found：鉴权已过，模型没找到
    # —— 账号被策略拦截 ——
    11140: "restricted",      # request illegal / 内容安全审查未通过
    # —— 模型维度 ——
    11133: "probe_defect",    # 请求参数被模型提供方拒绝
    11200: "quota",           # 无该模型权限 / 额度不足
    # —— 请求形态 ——
    11101: "probe_defect",    # Non-stream chat request is currently not supported
    11103: "invalid_model_name",  # 模型名格式不合法
}

# 语义类别 → 人话解释。界面据此说明结论是怎么得出来的，
# 而不是只丢一个「凭据有效」让人对着日志里的 403 发懵。
SEMANTIC_LABELS: Dict[str, str] = {
    "auth": "登录已过期，需要重新登录",
    # 风控就说「风控」。这是用户能对上号的词 —— 它直接指向
    # 「账号被平台限制了」，而不是让人以为工具或网络出了问题。
    "restricted": "账号被上游风控拦截",
    "quota": "该账号对这个模型没有权限或额度不足",
    # 探测刻意用一个**不存在的模型名**：上游回「模型不存在」正是它已认下凭据的证据。
    # 旧文案直接写「上游以『模型不存在』应答」，用户看到「不存在」三个字就读成故障，
    # 明明是正常账号却像出了问题。这里只说明结论与理由，不再复述上游的措辞。
    "model_missing": "账号正常",
    "probe_defect": "这次探测没被上游接受，没能得出结论",
    "invalid_model_name": "探测用了不合法的模型名，未能得出结论",
    "ok": "调用链路正常",
    "transient": "网络或上游临时异常，没能得出结论",
}

# 探测**手段**的说明。上面那些标签只讲结论，这里补上「怎么测出来的」，
# 供排查时参考 —— 分开存放是为了让结论文案保持干净。
# **界面默认不展示这一句**：正常账号的提示里不该出现探测细节，
# 「用一个不存在的模型名试」听起来就像故障，会把好结论读坏。
PROBE_METHOD_NOTE = "用官方的鉴权方式试了一次，上游认下了这张凭据"

# 响应体里出现这些片段时的兜底归类（错误码取不到或不在表里时用）。
# 典型来源：给上游传了超出取值范围的可选参数（如 max_tokens 低于最小值）。
PROBE_DEFECT_HINTS = (
    "integer_below_min_value",
    "integer_above_max_value",
    "invalid_parameter",
    "string_too_long",
    "string_too_short",
    "not supported",
)
# 「账号被策略拦截」的文字特征。这些响应常常**没有**错误码，
# 只能靠文案识别 —— 不识别的话就会被当成凭据失效（403）而误导用户重新登录。
RESTRICTED_HINTS = (
    "request illegal",
    "safety review",
    "safety_review",
    "content policy",
    "risk control",
    "risk_control",
    "violat",
)
# 「模型不存在」的文字特征。对凭据探测而言这是**好消息**（鉴权已过）。
MODEL_MISSING_HINTS = (
    "invalid model",
    "model not found",
    "unknown model",
    "unsupported model",
    "model_not_found",
    "model service info not found",
)

# 兼容旧名：过去按「错误码集合」判探测缺陷，现在统一走语义表。
PROBE_DEFECT_CODES = {c for c, s in _CODE_SEMANTICS.items() if s == "probe_defect"}

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
        # 是否在探模型之前先探一次账号凭据。开启可省掉「凭据失效账号」下
        # 那几十次注定失败的模型探测，也能把「token 过期」与「模型坏了」区分开。
        "checkAccounts": True,
        # 凭据确认失效时，是否自动停用整个账号。默认开启 —— 一个凭据失效的账号
        # 每次被轮询到都必然失败，让它继续留在轮询里只会拖慢请求。
        # 注意这只在「确认失效」（401/403）时触发，网络抖动不算。
        "autoDisableAccounts": True,
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
    if "checkAccounts" in raw:
        cfg["checkAccounts"] = bool(raw.get("checkAccounts"))
    if "autoDisableAccounts" in raw:
        cfg["autoDisableAccounts"] = bool(raw.get("autoDisableAccounts"))

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
                    "onlyUsedModels", "autoEnable", "logResults",
                    "checkAccounts", "autoDisableAccounts"):
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
    semantic = classify_semantic(code, snippet)
    return semantic in ("probe_defect", "invalid_model_name")


def classify_semantic(code: Optional[int], snippet: Optional[str] = None) -> Optional[str]:
    """把一次失败响应归到 ``_CODE_SEMANTICS`` 里的某个类别；分不清则返回 None。

    判定顺序（先精确后模糊）：

    1. 上游错误码命中语义表 —— 最可靠的证据；
    2. 错误码不在表里（上游新增/变更）时，按响应体文字特征兜底；
    3. 都识别不出 → None，交给调用方按状态码粗判。

    **状态码在这里刻意不参与**：403 至少对应三种完全不同的原因
    （token 过期、内容审查、无模型权限），只看状态码必然误判。
    """
    if code is not None and code in _CODE_SEMANTICS:
        return _CODE_SEMANTICS[code]
    text = (snippet or "").lower()
    if not text:
        return None
    if any(h in text for h in RESTRICTED_HINTS):
        return "restricted"
    if any(h in text for h in MODEL_MISSING_HINTS):
        return "model_missing"
    if any(h in text for h in PROBE_DEFECT_HINTS):
        return "probe_defect"
    return None


def classify_probe(status_code: Optional[int], error: Optional[str] = None,
                   code: Optional[int] = None,
                   snippet: Optional[str] = None) -> str:
    """把一次探测结果归类为 available / unavailable / transient / probe_defect / restricted。

    - 正常返回 → ``available``
    - 上游拒绝了我们的请求参数 → ``probe_defect``，说明探测形态不对，
      **不能**据此判定模型好坏，不参与自动禁用
    - 账号被上游策略拦截（内容审查 / 风控）→ ``restricted``。
      这不是模型的问题，更不是凭据的问题 —— 同样不参与自动禁用，
      否则一次内容审查就会把这个账号名下的模型成批误禁。
    - 限流 / 超时等**临时**状态 → ``transient``，不参与自动禁用
    - 明确的功能性失败（模型不存在、无权限、上游 5xx）→ ``unavailable``

    注意 ``restricted`` 是后加的类别：过去它落在 ``unavailable`` 里，
    再叠加 ``run_round`` 的「整账号都失败 → 凭据失效」推断，
    最终表现为「所有账号凭据有效，但巡检说三个账号凭据失效」这种自相矛盾的结论。
    """
    if error:
        return "transient"
    if status_code is None:
        return "transient"
    if 200 <= status_code < 300:
        return "available"
    # 先于状态码判断：400 既可能是「模型不可用」，也可能是「请求参数不对」，
    # 后者绝不能当成模型的问题 —— 否则一次参数改动就能成批误禁好模型。
    semantic = classify_semantic(code, snippet)
    if semantic in ("probe_defect", "invalid_model_name"):
        return "probe_defect"
    if semantic == "restricted":
        return "restricted"
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
    semantic = classify_semantic(code, snippet)
    verdict = classify_probe(status_code, error, code, snippet)
    return {
        "model": model,
        "status": status_code,
        "verdict": verdict,          # available / unavailable / transient / probe_defect / restricted
        "code": code,                # 上游错误码，仅用于诊断
        "semantic": semantic,        # 错误码对应的语义类别，界面据此解释结论
        "error": error,
        "elapsedMs": int((time.time() - started) * 1000),
    }


def probe_account(client: Any, base_url: str, token: str,
                  timeout: float = 30.0) -> Dict[str, Any]:
    """只探「这个账号的凭据还有效吗」，不碰任何模型。

    发一个刻意不成立的请求（不存在的模型名），只看上游怎么回：

    - 鉴权类错误（401 / 错误码 11100）→ 凭据已失效，这个账号现在无论如何都调不通；
    - 「模型不存在」类错误 → **鉴权已经过了**，凭据有效；
    - 账号被策略拦截（内容审查 / 风控）→ 凭据有效，但请求发不出去。**不是**凭据问题；
    - 请求形态被拒（非流式 / 参数越界）→ 探测自己的错，不构成任何凭据结论；
    - 连不上 / 超时 / 5xx → 探测没得到有效结论，不据此下判断。

    这样做的价值在于：一个凭据失效的账号，它名下**所有**模型都会失败。
    先花一次请求确认凭据，就能省掉后面几十次注定失败的探测，
    也避免把「凭据过期」误读成「这些模型全都坏了」而批量误禁。

    请求体与真实调用**完全同构**（``stream: True`` + system 首条 + 不传可选参数），
    这一点是必须遵守的：上游对非流式请求回 400 ``Non-stream chat request is not
    supported``，而 400 又落在「模型不存在」的判定区间里 —— 于是探测**从未真正
    到达判定点**，却回报「凭据有效」。这种假阳性会把失效账号读成好账号，
    比误报失效危险得多（失效账号被继续拿去轮询，请求全数失败）。
    """
    started = time.time()
    # 用不可能存在的模型名，确保请求不会真的产生一次生成；
    # 其余字段一律复用 build_probe_body，与模型探测保持同一形态，
    # 避免两处请求体各自演化、再次出现「同一账号两种结论」。
    body = build_probe_body("__wb_probe_nonexistent__")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status_code: Optional[int] = None
    error: Optional[str] = None
    snippet = ""
    try:
        status_code, snippet = _send_probe(client, f"{base_url}/chat/completions",
                                           body, headers, timeout)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    code = _extract_error_code(snippet)
    semantic = classify_semantic(code, snippet)
    verdict = classify_account_probe(status_code, error, snippet, code)
    return {
        "status": status_code,
        "verdict": verdict,          # available / auth_failed / restricted / transient / probe_defect
        "code": code,
        "semantic": semantic,
        "error": error,
        "elapsedMs": int((time.time() - started) * 1000),
    }


def classify_account_probe(status_code: Optional[int], error: Optional[str],
                           snippet: str = "",
                           code: Optional[int] = None) -> str:
    """把账号探测的响应归为 available / auth_failed / restricted / transient / probe_defect。

    注意与模型探测的**语义差别**：这里 400/404 是好消息 ——
    上游能说出「这个模型不存在」，说明它已经认下了这个 token。

    判定一律先查 ``_CODE_SEMANTICS`` 语义表，**不**按状态码直接下结论：

    - 凭据失效**只认** 401 或语义 ``auth``。403 不再一律当失效 ——
      它同样可能意味着内容审查，那说明凭据是好的。
    - 语义 ``restricted`` → ``restricted``：凭据有效但被策略拦截，
      既不能提示「重新登录」（登录也没用），也不能当作凭据失效。
    - 语义 ``probe_defect`` / ``invalid_model_name`` → ``probe_defect``：
      探测请求自己有问题，得不出任何凭据结论。**不能**当有效 ——
      那样失效账号会被读成好账号。
    """
    if error:
        return "transient"
    if status_code is None:
        return "transient"
    semantic = classify_semantic(code, snippet)
    if semantic == "auth":
        return "auth_failed"
    if semantic == "restricted":
        return "restricted"
    if semantic in ("probe_defect", "invalid_model_name"):
        return "probe_defect"
    if semantic == "model_missing":
        return "available"
    # 语义表没覆盖时才退回状态码判断。
    if status_code == 401:
        # 401 只有一个含义：没有有效的凭据。
        return "auth_failed"
    if status_code in TRANSIENT_STATUS:
        return "transient"
    if 200 <= status_code < 300:
        # 探测用的模型名不存在，正常上游不会返回 2xx。
        # 真返回了说明上游对未知模型很宽容 —— 凭据至少是通的，按有效处理。
        return "available"
    # 400/403/404/422 这类「上游听懂了请求、并且拒绝了它」的响应，
    # 说明鉴权这一关已经过了（否则不会走到业务校验）。
    # 但要先排除文字特征已表明是内容审查的情况 —— 那归 restricted。
    lowered = (snippet or "").lower()
    if any(h in lowered for h in RESTRICTED_HINTS):
        return "restricted"
    if status_code in (400, 403, 404, 422):
        return "available"
    if any(h in lowered for h in MODEL_MISSING_HINTS):
        return "available"
    # 5xx 之类：分不清是上游整体故障还是这个账号的问题，不下结论。
    return "transient"


# ---------------------------------------------------------------------------
# 结果落地到禁用策略
# ---------------------------------------------------------------------------

def apply_verdicts(policy: Dict[str, Any], account_id: str, results: List[Dict[str, Any]],
                   auto_enable: bool = True, account_blocked: bool = False) -> Dict[str, Any]:
    """把一轮探测结果写回策略。

    - ``unavailable`` 且当前未禁用 → 标记为 ``auto`` 禁用
    - ``unavailable`` 且已被禁用 → 保持原来源不变（手动禁用不会被改写为 auto）
    - ``available`` 且 ``auto_enable`` 且来源是 ``auto`` → 移除（自愈）
    - ``available`` 但来源是 ``manual`` → **不动**，手动禁用受保护
    - ``transient`` / ``probe_defect`` / ``restricted`` → **不动**（探测没得到有效结论）

    ``restricted``（账号被内容审查 / 风控拦截）必须保持原样：它说明请求没被放行，
    而不是模型坏了。据它禁用模型，等于让一次风控拦截把账号名下模型成批标坏。

    ``account_blocked``：账号级探测判定为「受限」（风控 / 内容审查）时为 True。
    此时**禁止一切自动启用** —— 账号整体被拦的情况下，个别模型偶然探测通过
    不代表它能用，把它放回轮询只会继续失败。风控是账号级状态，
    必须等账号级探测重新判定为正常，才谈得上放开模型。
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
            elif auto_enable and not account_blocked:
                entry.pop(model, None)
                enabled_now.append(model)
            else:
                unchanged.append(model)
        else:
            # transient / probe_defect / restricted：没拿到有效结论，一律保持原样。
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


def credential_state(acc_probe: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把账号探测的原始结果翻译成界面/接口可直接引用的凭据状态。

    存在的意义是**统一口径**：账号健康状况只有一个来源（账号级探测），
    界面不必再去模型级结果里反推，也就不会再出现
    「同一个账号在这里说有效、在那里说失效」。
    """
    if not acc_probe:
        return {
            "state": "unknown",
            "label": "未检测",
            "detail": "本轮未做账号级探测",
            "semantic": None,
            "code": None,
        }
    verdict = acc_probe.get("verdict")
    semantic = acc_probe.get("semantic")
    code = acc_probe.get("code")
    state = {
        "available": "valid",
        "auth_failed": "invalid",
        "restricted": "restricted",
        "probe_defect": "unknown",
        "transient": "unknown",
    }.get(verdict, "unknown")
    label = {
        "valid": "账号正常",
        "invalid": "登录已过期",
        "restricted": "账号被风控",
        "unknown": "没测出结果",
    }[state]
    # 说明文案优先用语义表里的解释（它带上了判定依据），
    # 语义识别不出来时退回 verdict 的通用说明。
    detail = SEMANTIC_LABELS.get(semantic or "") or {
        "available": SEMANTIC_LABELS["model_missing"],
        "auth_failed": SEMANTIC_LABELS["auth"],
        "restricted": SEMANTIC_LABELS["restricted"],
        "probe_defect": SEMANTIC_LABELS["probe_defect"],
        "transient": SEMANTIC_LABELS["transient"],
    }.get(verdict, "未得出结论")
    # 每档一句**可执行**的建议，措辞尽量短 —— 它会被放在提示里给人看，
    # 长篇解释没人读完。只保留「该做什么」，不再复述结论
    #（结论已经由 userMessage 说过，重复一遍等于让人读两遍同一件事）。
    action = {
        "valid": None,
        # userMessage 已经说了「请重新登录」，这里不再重复一遍。
        "invalid": None,
        # 风控的关键信息只有一个：重新登录没用。
        # 过去这一句后面还跟着「请检查账号状态或联系上游，也可先停用该账号避免
        # 占用轮询」，把一件小事写成了一段话 —— 而其中「先停用」巡检已经自动做了。
        "restricted": "重新登录没用，需要确认账号状态",
        "unknown": "稍后重试",
    }.get(state)
    # 面向用户的整句提示。给界面直接显示用，**不暴露任何探测细节**。
    #
    # 为什么单独一个字段：界面此前把 `method`（探测手段）拼在结论后面，
    # 于是正常账号的提示成了「凭据有效（用一个不存在的模型名试…）」——
    # 用户读到的重点变成「不存在的模型名」，像在报故障，而结论其实是好的。
    # 探测怎么做的属于实现细节，用户只关心「我的账号能不能用」。
    user_message = {
        "valid": "账号正常，可以放心使用",
        "invalid": "登录已过期，请重新登录",
        # 风控要说成风控：用户看到「风控」立刻明白是自己账号的状态问题，
        # 而不是以为工具坏了。这也是唯一能让人做出正确处置的说法。
        "restricted": "账号被上游风控拦截，暂时用不了",
        "unknown": "这次没测出结果，请稍后重试",
    }.get(state, "未得出结论")
    return {
        "state": state,
        "label": label,
        "detail": detail,
        # 界面优先显示这个字段：一句人话，无技术细节。
        "userMessage": user_message,
        # 探测手段的说明只对「有效」这一档给出：其余几档的 detail 本身
        # 就在讲哪里不对，再补一句「怎么测的」只会更长。
        # 分开一个字段，是为了让界面能选择要不要展示 —— 结论文案保持干净，
        # 需要解释时再用这一句。**界面默认不展示它**。
        "method": PROBE_METHOD_NOTE if state == "valid" else None,
        "action": action,
        "semantic": semantic,
        "code": code,
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

    另外**手动禁用的组合压根不探测**：那是人的明确决定，探测它得不到任何有用的
    结论，只会白花上游额度，并让「人禁用的」和「巡检禁用的」在界面上混成一锅。
    与之相对，``auto`` 项必须继续探测 —— 自愈正是靠这轮探测发现模型恢复可用。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    policy = model_policy.load_policy()
    combos = select_targets(accounts, config, used_models, base_models)

    skip_manual: Dict[str, Set[str]] = {}
    kept: List[Dict[str, str]] = []
    for combo in combos:
        acc_id = combo["accountId"]
        if acc_id not in skip_manual:
            skip_manual[acc_id] = model_policy.manual_disabled_for(acc_id, policy)
        model = combo["model"]
        blocked = skip_manual[acc_id]
        if model in blocked or model_policy.MODEL_ALIAS_MAP.get(model, model) in blocked:
            continue
        kept.append(combo)
    skipped_manual = len(combos) - len(kept)
    combos = kept

    by_id = {}
    for acc in accounts:
        acc_id = acc.get("id") or acc.get("uid")
        if acc_id:
            by_id[str(acc_id)] = acc

    account_reports: List[Dict[str, Any]] = []
    counts = {"available": 0, "unavailable": 0, "transient": 0,
              "probe_defect": 0, "restricted": 0}
    auth_failed: List[str] = []
    defect_codes: Set[int] = set()
    # 账号级探测结果：``{account_id: verdict}``。
    # 与模型级 counts 分开记 —— 账号级探的是凭据，混进模型计数会让
    # 「本轮多少组合可用」这个数字失去意义。
    account_probes: Dict[str, str] = {}
    check_accounts = bool(config.get("checkAccounts", True))

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

            # 先花一次请求确认凭据，再决定要不要接着探模型。
            #
            # 无论名下有几个模型都先探账号级：它是凭据有效性的**唯一**来源，
            # 只探一个模型时更需要它 —— 否则那一个模型的失败就会被当成凭据问题。
            #
            # 探测没得到结论（transient / probe_defect）时照常往下探模型：
            # 一次探测失败就跳过，等于让探测侧的问题变成账号的问题。
            acc_probe: Optional[Dict[str, Any]] = None
            if check_accounts:
                acc_probe = probe_account(client, base_url, token)
                account_probes[acc_id] = acc_probe["verdict"]

            # 凭据确认失效：跳过后面的模型探测。它名下所有模型都会失败，
            # 逐个探只是白花额度，而且会让「token 过期」看起来像「模型全坏了」。
            if acc_probe and acc_probe["verdict"] == "auth_failed":
                auth_failed.append(acc_id)
                existing = sorted(model_policy._coerce_entry(policy.get(acc_id)) or {})
                account_reports.append({
                    "accountId": acc_id,
                    "accountName": acc.get("nickname") or acc.get("email") or acc_id,
                    "disabled": [],
                    "enabled": [],
                    "protected": [],
                    "unchanged": sorted(models),
                    "disabledBefore": existing,
                    "disabledAfter": existing,
                    "changed": False,
                    "authFailed": True,
                    # 没能探到模型级结论：跳过的原因必须在面板上说得清，
                    # 否则用户只会看到「组合数比模型总数少」却不知道差在哪。
                    "skippedReason": "凭据已失效，未再逐个探测其模型",
                    "accountProbe": acc_probe,
                    "credential": credential_state(acc_probe),
                    "results": [],
                })
                continue

            # 账号被策略拦截（内容审查 / 风控）：凭据是好的，但它发出的请求会被拦。
            # 此时**不跳过**模型探测 —— 目的是把「全账号被拦」这个事实测出来，
            # 让报告里出现一个明确的 restricted 状态，而不是留下一串
            # 没有解释的 unavailable。这些失败会照常走 apply_verdicts 里的保护逻辑。
            results = []
            for model in models:
                item = probe_once(client, base_url, token, model)
                counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
                if item.get("verdict") == "probe_defect" and item.get("code") is not None:
                    defect_codes.add(int(item["code"]))
                results.append(item)

            existing = sorted(model_policy._coerce_entry(policy.get(acc_id)) or {})
            # 凭据有效性**只由账号级探测裁定**，不再从模型级结果反推。
            #
            # 这里曾有一条「整账号的模型都栽在 401/403 上 → 判凭据失效」的推断，
            # 它制造过一组自相矛盾的结论：同样三个账号，「检测账号」说凭据有效，
            # 巡检却说凭据失效。原因是那些 403 其实是**内容安全审查**
            # （错误码 11140 / request illegal）—— 凭据好好的，只是请求被拦。
            # 从「模型请求失败」反推「凭据坏了」在逻辑上就不成立：
            # 失败可以有多个原因，凭据只是其中之一，而账号级探测能把它单独测出来。
            #
            # 所以：模型级失败一律按模型维度处理（该禁则禁），
            # 账号健康状态看上面账号级探测的结论。
            report = apply_verdicts(policy, acc_id, results,
                                    auto_enable=bool(config.get("autoEnable", True)),
                                    account_blocked=(acc_probe or {}).get("verdict") == "restricted")
            report["accountName"] = acc.get("nickname") or acc.get("email") or acc_id
            report["accountProbe"] = acc_probe
            report["results"] = results
            # 账号级结论单独摆放，供界面与接口直接引用，
            # 不必再去 results 里猜（那是模型维度的数据）。
            report["credential"] = credential_state(acc_probe)
            account_reports.append(report)
    finally:
        try:
            client.close()
        except Exception:
            pass

    # 整轮保护：一个可用的都没有，而且失败集中在「探测机制本身有问题」或
    # 「账号被策略拦截」这两类上，几乎可以肯定是探测侧失效
    # （凭据、请求形态、上游整体故障、风控拦截），而不是所有模型同时坏掉。
    # 此时不写策略 —— 成批误禁要靠人工逐个放开，代价远高于漏禁一轮。
    #
    # restricted（内容审查）必须计入：它曾被算作「模型不可用」，
    # 于是一次风控拦截就会把一批模型标成坏的。
    no_signal = counts["unavailable"] + counts["probe_defect"] + counts["restricted"]
    aborted = counts["available"] == 0 and no_signal >= GUARD_MIN_UNAVAILABLE
    # 「实际落盘的变更」——整轮作废时不写策略，两者都保持为空。
    applied_disabled: List[str] = []
    applied_enabled: List[str] = []
    # 自动停用的账号 id（来自凭据探测）。整轮作废时同样不写。
    applied_accounts_disabled: List[str] = []
    applied_accounts_enabled: List[str] = []
    if aborted:
        # 逐账号明细里的变更声明要一并清空：策略没有落盘，
        # 留着会让人以为「这些已经被禁了」，而实际什么都没写。
        for report in account_reports:
            report["disabled"] = []
            report["enabled"] = []
            report["changed"] = False
        if counts["restricted"] >= GUARD_MIN_UNAVAILABLE:
            reason = (
                f"本轮 {counts['restricted']} 个组合被上游内容安全审查拦下"
                "（request illegal / safety review），凭据本身没有问题，"
                "已跳过写入。请检查这些账号是否被上游风控标记。"
            )
        elif counts["probe_defect"] >= GUARD_MIN_UNAVAILABLE:
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
        # 落盘前**重新读一遍策略**，只把本轮的判定叠加上去，而不是把开场读到的快照整份写回。
        #
        # 一轮巡检要跑几分钟（几百次探测），开场读入的文件在这段时间里可能已经被改过 ——
        # 用户在卡片上点下的手动禁用就是一例。把旧快照整份写回会把这些改动**静默抹掉**，
        # 而「巡检不得影响手动禁用」正是来源标记存在的意义。
        #
        # 叠加用的是 auto_disable / auto_enable，它们本身就带保护：
        #   - auto_disable 不会把已有的 manual 项改写成 auto；
        #   - auto_enable 只放得开 auto 项。
        # 于是「本轮探测到不可用」仍然生效，而人手点下的一律照旧。
        fresh = model_policy.load_policy()
        for report in account_reports:
            acc_id = report["accountId"]
            for model in report["disabled"]:
                if model_policy.auto_disable(fresh, acc_id, model):
                    applied_disabled.append(model)
            if bool(config.get("autoEnable", True)):
                for model in report["enabled"]:
                    if model_policy.auto_enable(fresh, acc_id, model):
                        applied_enabled.append(model)

        # 有实际改动才留底：一轮巡检可能同时改动上百个条目，
        # 判定万一有误，用户需要的是整份回退，而不是逐个点回来。
        # 无改动时不覆盖，好让更早的那份底尽可能久地留着。
        if applied_disabled or applied_enabled:
            model_policy.backup_policy(BACKUP_SUFFIX)
        model_policy.save_policy(fresh)

        # 账号级停用走**独立的文件与独立的开关**。
        #
        # 两类账号要被自动停掉：
        #   - ``auth_failed``：凭据确实失效。确定、可复现，重新登录才会好。
        #   - ``restricted``：账号被上游风控 / 内容安全拦下。凭据虽然有效，
        #     但它发出的请求一律被拦 —— 留在轮询里只会持续失败，
        #     还会把该账号名下的模型探测结果污染成一片失败。
        #     过去只停 auth_failed，风控账号照常参与轮询，这正是
        #     「巡检都测出风控了，却还在用」的来源。
        #
        # 而 ``transient``（网络抖动、上游 5xx）不下手：那是一次性的，
        # 停掉一个账号等于停掉它名下全部模型，误停的代价太大。
        if check_accounts and bool(config.get("autoDisableAccounts", True)):
            acc_policy = account_policy.load_policy()
            changed = False
            for acc_id, verdict in account_probes.items():
                if verdict not in ("auth_failed", "restricted"):
                    continue
                if account_policy.set_disabled(acc_policy, acc_id, True,
                                               account_policy.SOURCE_AUTO):
                    applied_accounts_disabled.append(acc_id)
                    changed = True
            if changed:
                account_policy.backup_policy(BACKUP_SUFFIX)
                account_policy.save_policy(acc_policy)

        # 账号级**自动启用**：只有账号探测明确判定为正常（valid）才放回轮询。
        #
        # 这一条同样重要，且方向与上面相反 —— 少了它，被风控停掉的账号
        # 即使已经恢复也没人把它放回来，只能人工干预。
        # 但判据必须是**账号级结论**，不能是「名下某个模型探测通过了」：
        # 账号整体被风控时，个别模型偶然通过不代表它能用。
        # 也只放得开 auto 来源，人工停用的账号巡检无权替人放开。
        if check_accounts and bool(config.get("autoEnableAccounts", True)):
            acc_policy = account_policy.load_policy()
            changed = False
            for acc_id, verdict in account_probes.items():
                if verdict != "available":
                    continue
                if account_policy.auto_enable(acc_policy, acc_id):
                    applied_accounts_enabled.append(acc_id)
                    changed = True
            if changed:
                account_policy.backup_policy(BACKUP_SUFFIX)
                account_policy.save_policy(acc_policy)

    result = {
        "checkedAt": now_ms,
        "combos": len(combos),
        "accounts": len(grouped),
        # 因「手动禁用」而整个未探测的组合数。界面据此说明这一轮为什么没覆盖它们，
        # 否则用户只会看到组合数比模型总数少，却不知道差在哪。
        "skippedManual": skipped_manual,
        "counts": counts,
        # 变更列表取**实际落盘的结果**，不是本轮的意图：叠加到最新策略上之后，
        # 某项可能因为用户刚刚手动禁用它而无需再写，如实反映才不会谎报。
        "disabled": sorted(set(applied_disabled)),
        "enabled": sorted(set(applied_enabled)),
        "protected": sorted({m for r in account_reports for m in r["protected"]}),
        # 凭据失效被跳过的账号：界面据此提示「请重新登录」，
        # 而不是让人对着「一轮下来什么都没变」的结果猜原因。
        "authFailed": [
            {"id": r["accountId"], "name": r.get("accountName")}
            for r in account_reports if r.get("authFailed")
        ],
        # 账号被上游策略拦截（内容审查 / 风控）的账号。
        # 与 authFailed 分开列：凭据是好的，提示「重新登录」会把人引向错误的方向。
        "restricted": [
            {"id": r["accountId"], "name": r.get("accountName"),
             "detail": (r.get("credential") or {}).get("detail")}
            for r in account_reports
            if (r.get("credential") or {}).get("state") == "restricted"
        ],
        # 本轮被自动停用的账号（凭据确认失效 / 账号被风控）。界面据此说明账号为什么不再参与调用。
        "accountsDisabled": sorted(set(applied_accounts_disabled)),
        # 本轮被自动放回轮询的账号（账号级探测判定恢复正常）。
        # 独立列出的意义：用户需要知道「某个账号为什么又回来了」，
        # 而不是某天发现它悄悄参与了调用。
        "accountsEnabled": sorted(set(applied_accounts_enabled)),
        # 账号级探测结果，界面用来区分「凭据坏了」还是「模型坏了」。
        "accountProbes": account_probes,
        # 账号健康状况汇总：{valid / invalid / restricted / unknown: 账号数}。
        # 这是「统一标准」在结果里的体现 —— 账号层面的结论只有这一处口径。
        "accountHealth": {
            state: sum(
                1 for r in account_reports
                if (r.get("credential") or {}).get("state") == state
            )
            for state in ("valid", "invalid", "restricted", "unknown")
        },
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
    """给界面用的精简摘要（不带逐条探测明细，避免响应体过大）。

    这份摘要会**落盘**（``model_health_last.json``），进程重启后界面直接读它回显。
    因此这里必须把界面上会用到、而重跑一轮也补不回来的字段全部带上：
    ``authFailed``（凭据失效账号名单）与 ``defectCodes``（探测被拒的错误码）
    都只在那一轮里存在，漏掉它们，重启后这两个提示就永远不再出现。

    （此前这里有两份同名定义，后一份把 ``authFailed`` / ``defectCodes`` 丢掉了 ——
    Python 只保留最后一份，于是落盘的摘要长期缺这两个字段。下面只保留一份。）
    """
    return {
        "checkedAt": round_result.get("checkedAt"),
        "combos": round_result.get("combos", 0),
        "accounts": round_result.get("accounts", 0),
        "skippedManual": round_result.get("skippedManual", 0),
        "counts": round_result.get("counts", {}),
        "disabled": round_result.get("disabled", []),
        "enabled": round_result.get("enabled", []),
        "protected": round_result.get("protected", []),
        "authFailed": round_result.get("authFailed", []),
        # 「账号被内容审查 / 风控拦截」与「凭据失效」是两种完全不同的状态，
        # 界面提示的方向也相反（一个要重新登录，一个登录也没用），
        # 所以必须分别落盘，不能只留 authFailed。
        "restricted": round_result.get("restricted", []),
        "accountHealth": round_result.get("accountHealth", {}),
        "accountsDisabled": round_result.get("accountsDisabled", []),
        "defectCodes": round_result.get("defectCodes", []),
        "aborted": bool(round_result.get("aborted")),
        "reason": round_result.get("reason"),
    }
