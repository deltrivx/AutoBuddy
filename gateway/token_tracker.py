"""Token 用量流水与聚合。

这个模块同时是官方前端「Token 统计」页的数据源：WebUI 代理会用这里的
``get_aggregated_token_stats()`` 接管 ``/api/token-stats``。

**缓存命中率的算法在官方前端里，不在我们手里**，它读的是每天 / 每个模型的
``cacheRead`` 与 ``input``：

    cacheHitRate = cacheRead / input

（``input`` 是该桶的完整输入 token，含命中缓存的部分；缺失的日期会被前端补
``cacheRead: 0`` 再用同一个公式重算。）因此这边**只要把 cacheRead 填对**，
界面上就对了；以前这里恒填 0，命中率自然永远是 0 —— 不是算法错，是数据没采。

缓存的三个数从上游响应的 ``usage`` 里取，字段名各版本不完全一致，见
``extract_usage()``。
"""

import atexit
import json
import os
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

# token_store 的导入必须容忍两种运行方式 —— 与本项目其它模块
# （main.py / web_proxy.py）的既有约定保持一致：
#
#   · 容器内以顶层模块运行：sys.path 含 /app/gateway  → import token_store
#   · 以包形式导入          ：                        → from gateway import token_store
#
# 这里写成硬 ``from gateway import token_store`` 会在第一种模式下
# ModuleNotFoundError —— 网关一启动就崩。所以两种都试。
try:
    from gateway import token_store
except ImportError:  # pragma: no cover - 取决于运行方式
    import token_store  # type: ignore[no-redef]

TRACKER_FILE = Path("/data/.autobuddy/token_stats_logs.json")

# Token 用量的存储后端。
#
# 历史上明细就存在上面的 TRACKER_FILE（单个 JSON），每次记录都要
# 「读整个文件 → append → 写整个文件」，窗口写满时约 1.28MB 一轮 I/O。
# v0.9.27 用攒批降低了频率，但单次落盘仍是整文件重写，成本没变。
#
# v0.9.29 起默认改走 SQLite（gateway/token_store.py）：写入变成增量
# INSERT（幂等），聚合查询有复合索引。聚合逻辑与输出契约**一行未改**，
# 换的只是「明细从哪来、往哪写」。
#
# 开关保留是为了可回滚：设 AB_TOKEN_STORE=json 即回到旧行为。
TOKEN_STORE = (os.getenv("AB_TOKEN_STORE", "sqlite") or "sqlite").strip().lower()

# 明细保留策略。
#
# sub2api 的关键提醒：**别用滑动窗口**。
# 「保留最近 N 条」本质是行数上限 —— 请求一密，历史跨度就被数据量绑架
# （本网关实测每天约 2000 条调用，3000 条的上限不到两天就写满），
# 这正是「词元统计只剩两天」的根因。
#
# 所以主维度改成**时间**：默认保留 7 天，历史跨度与调用量彻底解耦。
# 行数上限降级为文件体积的兜底护栏，只在时间维度失效时才起作用。
DETAIL_MAX_DAYS = int(os.getenv("AB_TOKEN_DETAIL_DAYS", "7") or 7)
# 护栏：单个明细文件的体积上限（条数）。防的是某天调用量异常暴涨
# 把文件撑到不可控 —— 正常情况由上面的时间维度先兜住。
MAX_DETAIL = int(os.getenv("AB_TOKEN_DETAIL_MAX", "3000") or 3000)

# ---------------------------------------------------------------------------
# 批量落盘
#
# 参考 sub2api（43k stars）的做法：它的用量写入不是逐条同步落库，而是
# 攒批 —— 满 64 条或 3ms 窗口到了才真正写一次（best-effort 通道是
# 256 条 / 20ms）。
#
# 这里同理：每次请求都重写整个明细文件，等于每请求一次 1.28MB 的
# 读+写（实测窗口写满时 3000 条 ≈ 1.28MB），请求一密就把 I/O 打满。
# 改成内存攒批 + 到量/到点才落盘，每请求的摊还成本就降下来了。
# ---------------------------------------------------------------------------
PENDING_FLUSH_MAX = int(os.getenv("AB_TOKEN_PENDING_MAX", "32") or 32)
PENDING_FLUSH_SEC = float(os.getenv("AB_TOKEN_PENDING_SEC", "1.0") or 1.0)
# 落盘失败时回灌队列的上限，避免磁盘满时内存无界增长
_PENDING_MAX_BACKLOG = PENDING_FLUSH_MAX * 8

# 聚合行保留天数（分级保留，对齐 sub2api 的分档思路，只是档位少一些）
ROLLUP_MAX_DAYS = int(os.getenv("AB_TOKEN_ROLLUP_DAYS", "90") or 90)

_PENDING_LOCK = threading.Lock()
_PENDING: List[Dict[str, Any]] = []
_LAST_FLUSH = 0.0


def _atomic_write_json(path: Path, payload: Any) -> None:
    """原子写 JSON：先写临时文件再 os.replace。

    直接 open(w) 写到一半进程被杀（容器重建/重启很常见），会留下半截
    JSON —— 下次读取整个统计页就炸了。replace 在同一文件系统内是原子操作，
    要么看到旧文件、要么看到新文件，不会看到写了一半的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _prune_rollup(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按天裁剪聚合行，只保留 ROLLUP_MAX_DAYS 天内的数据。

    sub2api 的做法是分级保留（1m 明细 3 天、5m/1h/12h/1d 汇总 7/30/45/90 天）。
    这里档位少一些，但原则一致：**越粗的档保留越久**，
    这样既能回溯长期趋势，又不会让聚合文件无限膨胀。
    """
    if ROLLUP_MAX_DAYS <= 0:
        return rows
    cutoff = (datetime.now() - timedelta(days=ROLLUP_MAX_DAYS)).strftime("%Y-%m-%d")
    return [r for r in rows if str(r.get("date") or "") >= cutoff]


def _split_expired(logs: List[Dict[str, Any]]) -> tuple:
    """按时间维度把明细拆成 (保留, 超期) 两组。

    为什么主维度必须是时间而不是条数：
    「保留最近 N 条」会让历史跨度随调用量浮动 —— 忙的时候只能看两天，
    闲的时候能看一个月。改成按天保留后，**能回溯多久是确定的**。

    没有日期字段的记录一律保留：宁可多留，也不能把数据误判成过期删掉。
    """
    if DETAIL_MAX_DAYS <= 0:
        return logs, []
    cutoff = (datetime.now() - timedelta(days=DETAIL_MAX_DAYS)).strftime("%Y-%m-%d")
    keep: List[Dict[str, Any]] = []
    expired: List[Dict[str, Any]] = []
    for rec in logs:
        d = str(rec.get("date") or "")
        if d and d < cutoff:
            expired.append(rec)
        else:
            keep.append(rec)
    return keep, expired


def flush_pending(force: bool = False) -> int:
    """把内存里攒的用量记录写入明细文件，返回本次落盘条数。

    平时由 record_token_usage 在「攒够一批或到点」时自动调用；
    进程退出前由 atexit 强制兜底一次，避免最后一批丢掉。

    落盘失败时把这一批**放回队列**（而不是静默丢弃）——
    统计宁可晚一点，也不能悄悄少数据。但队列有上限，
    磁盘真满了也不可能无限回灌。
    """
    global _LAST_FLUSH
    with _PENDING_LOCK:
        if not _PENDING:
            return 0
        if (not force
                and len(_PENDING) < PENDING_FLUSH_MAX
                and (time.time() - _LAST_FLUSH) < PENDING_FLUSH_SEC):
            return 0
        batch = list(_PENDING)
        _PENDING.clear()

    try:
        if TOKEN_STORE != "json":
            # SQLite：增量写入，不再是整文件重写
            _store().insert_batch(batch)
            # 保留策略按时间跑一次；不每次 flush 都扫全表 ——
            # 明细超期是以天为单位的，没必要每秒检查一次。
            _maybe_run_retention()
            return len(batch)

        # 旧路径（AB_TOKEN_STORE=json）：整文件读写
        logs: List[Dict[str, Any]] = []
        if TRACKER_FILE.exists():
            try:
                with open(TRACKER_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    logs = loaded
            except Exception:
                # 文件损坏（上次写一半被杀）宁可丢明细，也不能让统计页挂掉
                logs = []
        logs.extend(batch)
        # 先按**时间**裁剪：超期的明细折算进聚合，绝不直接丢。
        logs, expired = _split_expired(logs)
        if expired:
            fold_into_rollup(expired)
        # 再按行数护栏兜底（只在时间维度不够用时才会触发）
        if len(logs) > MAX_DETAIL:
            fold_into_rollup(logs[:-MAX_DETAIL])
            logs = logs[-MAX_DETAIL:]
        _atomic_write_json(TRACKER_FILE, logs)
    except Exception as e:
        print(f"Error flushing token usage: {e}")
        # 落盘失败时优先回灌队列；队列真的撑不住了，也不能**静默丢弃** ——
        # sub2api 的原则是「永不静默丢弃」（队列满时提交方内联执行，
        # 关停窗口的丢弃也会降级同步，保证计费不丢）。
        #
        # 这里退而求其次：把这一批折算进聚合再丢弃明细。
        # 明细没了，但 token 总数、调用次数、按天/按模型/按账号的分布
        # 全部保住 —— 统计页的数字仍然正确，只是查不到单次调用的详情。
        # 「悄悄少数据」和「降级保汇总」是两回事，后者才是可接受的兜底。
        with _PENDING_LOCK:
            if len(_PENDING) < _PENDING_MAX_BACKLOG:
                _PENDING[:0] = batch
                return 0
        try:
            # 兜底折算必须写进**当前后端**，不能固定写 JSON：
            # SQLite 模式下若写 JSON 聚合文件，读取侧从 SQLite 读，
            # 这批数据等于落到了没人看的地方 —— 同样是静默丢失。
            if TOKEN_STORE != "json":
                _store().fold_into_rollup(batch)
            else:
                fold_into_rollup(batch)
            print(f"token usage backlog full: folded {len(batch)} "
                  f"records into rollup (detail lost, totals preserved)")
        except Exception as fold_err:
            print(f"token usage fold failed: {fold_err}; "
                  f"{len(batch)} records dropped")
        return 0

    _LAST_FLUSH = time.time()
    return len(batch)


# 进程退出（容器 stop / 重建）前把最后一批落盘，别让末尾几秒的数据凭空消失
atexit.register(lambda: flush_pending(force=True))


_flush_thread_started = False
_flush_thread_lock = threading.Lock()


def _ensure_flush_thread() -> None:
    """启动后台落盘线程（进程内只起一次）。

    sub2api 的批量写入由「条数或时间窗口」两者先到触发，而**时间窗口是后台
    goroutine 自己的 ticker** —— 不依赖新请求到来。

    这里同样必须有后台线程：否则最后一批不足 PENDING_FLUSH_MAX 的记录会一直
    留在内存里，要等到下次统计页读取才落盘；万一进程被强杀（容器 SIGKILL
    很常见，atexit 根本不会执行），这批数据就彻底丢了。
    """
    global _flush_thread_started
    with _flush_thread_lock:
        if _flush_thread_started:
            return
        _flush_thread_started = True

    def _loop() -> None:
        while True:
            time.sleep(max(0.5, PENDING_FLUSH_SEC))
            try:
                flush_pending(force=True)
            except Exception:
                pass

    threading.Thread(target=_loop, name="token-usage-flusher",
                     daemon=True).start()


# 保留策略的检查间隔。明细超期是以**天**为单位的，没必要每次落盘都扫全表。
# 但也不能只在启动时跑一次 —— 长跑进程跨过午夜后日期就变了，
# 必须周期性重新判定。
_RETENTION_CHECK_SEC = float(os.getenv("AB_TOKEN_RETENTION_SEC", "3600") or 3600)
_last_retention_check = 0.0
_retention_lock = threading.Lock()


def _maybe_run_retention() -> None:
    """按节流间隔跑一次保留策略（超期明细折算进聚合后删除 + 裁剪聚合）。

    放在落盘路径里而不是单独起线程：落盘本来就会发生，
    顺手做一次检查几乎零成本，也省掉一个常驻线程。
    """
    global _last_retention_check
    now = time.time()
    if now - _last_retention_check < _RETENTION_CHECK_SEC:
        return
    with _retention_lock:
        # 双重检查：并发下只需一个线程真正执行
        if now - _last_retention_check < _RETENTION_CHECK_SEC:
            return
        _last_retention_check = now
    try:
        _store().expire_detail(DETAIL_MAX_DAYS)
        _store().prune_rollup(ROLLUP_MAX_DAYS)
    except Exception as e:
        # 保留策略失败不能连累主流程（数据还在，下次再试）
        print(f"Error running token retention: {e}")


def _store():
    """取存储后端，并把数据库文件对齐到 TRACKER_FILE 所在目录。

    测试会把 TRACKER_FILE 指到临时目录，数据库必须跟随 ——
    否则测试会写进生产库。
    """
    token_store.configure(TRACKER_FILE.parent / "token_stats.db")
    return token_store


def _rollup_path() -> Path:
    """历史聚合文件路径。

    跟随 TRACKER_FILE 所在目录（测试会把 TRACKER_FILE 指到临时目录），
    避免测试期间误写生产数据。
    """
    return TRACKER_FILE.parent / "token_stats_rollup.json"


def _load_rollup() -> List[Dict[str, Any]]:
    """读历史聚合行。文件缺失/损坏一律当成空，不让统计页挂掉。"""
    try:
        with open(_rollup_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def fold_into_rollup(evicted: List[Dict[str, Any]]) -> None:
    """把滑出明细窗口的记录，折算成「按天 x 模型 x 账号」的聚合行保存。

    为什么必须有这一步（用户 2026-10-02 反馈「词元统计只有最近两天」）：
    明细窗口只有 MAX_DETAIL 条，而实测网关每天产生约 2000 条调用 ——
    窗口不到两天就写满，最老的记录被直接丢掉，于是页面永远只剩两天。

    明细又不能无限涨：它每请求都要整文件读+写一次，
    3000 条已是 1.28MB 一轮 I/O，放大到几万条会把网关拖垮。

    所以做法是**明细保近期、历史转聚合**：滑出窗口的记录按维度
    加总成极小的一行（一天 x 模型 x 账号），既不丢历史，
    又让每请求的成本与「累积了多久」彻底无关。
    """
    if not evicted:
        return
    path = _rollup_path()
    try:
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []

        index = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            index[(r.get("date"), r.get("model"),
                   r.get("accountId"), r.get("variant"))] = r

        for rec in evicted:
            if not isinstance(rec, dict):
                continue
            key = (rec.get("date"), rec.get("model"),
                   rec.get("accountId") or "", rec.get("variant") or "")
            row = index.get(key)
            if row is None:
                row = {"date": key[0], "model": key[1],
                       "accountId": key[2], "variant": key[3],
                       "accountName": rec.get("accountName") or "",
                       "input": 0, "output": 0, "total": 0,
                       "cacheRead": 0, "cacheWrite": 0,
                       "uncachedInput": 0, "records": 0, "duration": 0.0}
                rows.append(row)
                index[key] = row
            inp = int(rec.get("input") or 0)
            out = int(rec.get("output") or 0)
            row["input"] += inp
            row["output"] += out
            row["total"] += inp + out
            row["cacheRead"] += int(rec.get("cacheRead") or 0)
            row["cacheWrite"] += int(rec.get("cacheWrite") or 0)
            row["uncachedInput"] += int(rec.get("uncachedInput") or 0)
            row["records"] += 1
            row["duration"] += float(rec.get("duration") or 0.0)
            if not row.get("accountName") and rec.get("accountName"):
                row["accountName"] = rec["accountName"]

        # 同 key 覆盖写回前先清掉旧的同 key 行，避免 append 造成重复行累积
        seen = set()
        deduped = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            k = (r.get("date"), r.get("model"), r.get("accountId"), r.get("variant"))
            if k in seen:
                continue
            seen.add(k)
            deduped.append(r)

        # 先按保留策略裁剪，再原子落盘（防写一半被杀留下半截文件）
        _atomic_write_json(path, _prune_rollup(deduped))
    except Exception as e:
        print(f"Error folding token history: {e}")


def _rec_count(rec: Dict[str, Any]) -> int:
    """一条参与聚合的记录代表多少次真实调用。

    明细记录代表 1 次；历史聚合行代表当时被折算掉的 N 次。
    不区分这点的计数会把历史压缩后的真实调用量算少。
    """
    try:
        return max(1, int(rec.get("records") or 1))
    except Exception:
        return 1


def extract_usage(obj: Any) -> Optional[Dict[str, int]]:
    """从上游响应里取出真实用量。

    上游（OpenAI 兼容）返回的 ``usage`` 里缓存字段有好几种写法，按可用性依次取：
    ``prompt_tokens_details.cached_tokens`` / ``cached_tokens`` /
    ``prompt_cache_hit_tokens`` / ``cache_read_input_tokens``。
    写盘缓存（cache write）同理取 ``cache_creation_input_tokens`` /
    ``prompt_cache_write_tokens``。

    取不到时返回 ``None``，由调用方回退到字符估算 —— **不要**在这里编一个 0 出来，
    「没有用量信息」和「缓存命中 0」是两件事。
    """
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None

    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}

    cache_read = 0
    for candidate in (details.get("cached_tokens"), usage.get("cached_tokens"),
                      usage.get("prompt_cache_hit_tokens"),
                      usage.get("cache_read_input_tokens")):
        if _int(candidate) > 0:
            cache_read = _int(candidate)
            break
    cache_write = 0
    for candidate in (usage.get("cache_creation_input_tokens"),
                      usage.get("prompt_cache_write_tokens")):
        if _int(candidate) > 0:
            cache_write = _int(candidate)
            break

    uncached = _int(usage.get("prompt_cache_miss_tokens"))
    if uncached <= 0:
        uncached = max(0, prompt - cache_read)

    return {
        "prompt": prompt,
        "completion": completion,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "uncached": uncached,
    }


class UsageScanner:
    """在流式透传的同时，从 SSE 里捞 ``usage`` 与正文长度。

    两个约束：

    1. **不能为了统计而缓冲整段响应**。因此只保留「最后一行没读完」的残余，
       正文只累加字符数，不留内容 —— 一次几十万 token 的长回复也不会把内存撑起来。
    2. **不能改请求形态**。用量是上游**本来就发**的末帧（实测不带
       ``stream_options`` 也会发），所以不需要往请求里加任何参数 ——
       这正是 v0.4.2/v0.4.3 巡检踩过两次的坑（多加一个可选参数就被上游校验拒掉）。
    """

    # 单行超过这个长度说明不是正常 SSE（或上游吐了脏数据），只留尾部，
    # 避免坏流把内存吊住。正常 usage 帧只有几百字节。
    MAX_TAIL = 64 * 1024

    def __init__(self) -> None:
        self._tail = ""
        self.text_chars = 0
        self.response_id = ""
        self.usage: Optional[Dict[str, int]] = None

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._tail += chunk.decode("utf-8", errors="ignore")
        if len(self._tail) > self.MAX_TAIL:
            self._tail = self._tail[-4096:]
        lines = self._tail.split("\n")
        self._tail = lines.pop()
        for line in lines:
            self._consume(line.strip())

    def _consume(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except Exception:
            return
        rid = obj.get("id")
        if isinstance(rid, str) and rid:
            self.response_id = rid
        found = extract_usage(obj)
        if found:
            self.usage = found
        choices = obj.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                delta = (choice or {}).get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str):
                    self.text_chars += len(content)

def record_token_usage(model: str, input_tokens: int, output_tokens: int, duration_sec: float = 0.0, request_id: str = "",
                       account_id: str = "", account_name: str = "", variant: str = "",
                       cache_read: int = 0, cache_write: int = 0):
    """网关实时调用记录。

    必须带上实际服务该请求的账号（account_id / account_name）：
    官方的按账号用量接口只对「当前账号」返回模型明细，其余账号的 models 恒为空，
    因此只有靠网关自己归因，账号卡片才能显示「其他账号调用了哪些模型」。

    ``cache_read`` / ``cache_write`` 来自上游 ``usage``。命中缓存的那部分输入
    同时计在 ``input``（完整输入）里，另记一份 ``uncachedInput``（未命中部分），
    这样前端 ``cacheRead / input`` 才是真实的命中比例。
    """
    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        ts = int(time.time() * 1000)

        cache_read = max(0, int(cache_read or 0))
        cache_write = max(0, int(cache_write or 0))
        # 上游偶尔会给出 cache_read > prompt_tokens 这种不一致的脏数据，夹一下，
        # 免得算出来一个超过 100% 的命中率。
        cache_read = min(cache_read, max(0, int(input_tokens or 0)))

        entry = {
            "id": request_id or f"wb-req-{ts}",
            "time": time_str,
            "ts": ts,
            "timestamp": ts,
            "date": date_str,
            "model": model,
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": cache_read,
            "cacheWrite": cache_write,
            "uncachedInput": max(0, int(input_tokens or 0) - cache_read),
            "total": input_tokens + output_tokens,
            "duration": round(duration_sec, 2),
            "accountId": account_id or "",
            "accountName": account_name or "",
            "variant": variant or "",
            "source": "gateway"
        }
        
        # 只入队，不在这里重写文件 —— 真正的落盘交给 flush_pending 攒批做，
        # 避免每请求一次整文件读写（详见 flush_pending 的说明）。
        with _PENDING_LOCK:
            _PENDING.append(entry)
            due = (len(_PENDING) >= PENDING_FLUSH_MAX
                   or (time.time() - _LAST_FLUSH) >= PENDING_FLUSH_SEC)
        if due:
            flush_pending()
        _ensure_flush_thread()
    except Exception as e:
        print(f"Error recording token usage: {e}")

def get_official_cloud_requests() -> List[Dict[str, Any]]:
    """从官方底层的 credits/stats 抓取云端真实流水，补充到本地流水中"""
    try:
        req = urllib.request.Request("http://127.0.0.1:57890/api/credits/stats")
        with urllib.request.urlopen(req, timeout=3.0) as r:
            data = json.loads(r.read().decode())
            return data.get("officialUsage", {}).get("requests", [])
    except Exception:
        return []

def parse_time_to_ts(time_str: str) -> int:
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)

def get_aggregated_token_stats() -> Dict[str, Any]:
    # 读取前先把内存里攒的记录落盘 —— 攒批是为了省写入 I/O，
    # 但「刚发生的调用在统计页看不到」是不能接受的。
    # 写侧攒批、读侧确保刷新，两端配合，既不费 I/O 又不丢实时性。
    try:
        flush_pending(force=True)
    except Exception:
        pass
    if TOKEN_STORE != "json":
        # 首次读取时把旧 JSON 的历史数据迁进来（幂等，只做一次）
        if TRACKER_FILE.exists():
            try:
                _store().migrate_from_json(TRACKER_FILE, _rollup_path())
            except Exception:
                pass
        logs = _store().fetch_detail(DETAIL_MAX_DAYS, MAX_DETAIL)
    else:
        logs = []
        if TRACKER_FILE.exists():
            try:
                with open(TRACKER_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except Exception:
                logs = []
            
    existing_ids = {l.get("id") for l in logs}
    cloud_reqs = get_official_cloud_requests()
    for cr in cloud_reqs:
        req_id = cr.get("requestId")
        if req_id and req_id not in existing_ids:
            credit = cr.get("credit", 0.0)
            m = cr.get("model", "unknown")
            t_str = cr.get("requestTime", "")
            d_str = t_str.split(" ")[0] if " " in t_str else datetime.now().strftime("%Y-%m-%d")
            ts = parse_time_to_ts(t_str)
            
            base_tokens = max(120, int(credit * 15000)) if credit > 0 else 180
            inp = int(base_tokens * 0.4)
            out = int(base_tokens * 0.6)
            
            logs.append({
                "id": req_id,
                "time": t_str,
                "ts": ts,
                "timestamp": ts,
                "date": d_str,
                "model": m,
                "input": inp,
                "output": out,
                "total": inp + out,
                "duration": 1.1,
                # 云端流水自带 accountId / accountName，必须带上：
                # 否则这些记录会全部落进「未归属账号」，把用量分布挤成 100% 一条。
                "accountId": cr.get("accountId") or "",
                "accountName": cr.get("accountName") or "",
                "variant": "",
                "source": "official"
            })
            existing_ids.add(req_id)

    # 明细只留近期，更早的调用已折算成聚合行存在 rollup 里。
    # 趋势图与汇总必须把历史一起算进去，否则页面永远只剩窗口内那一两天；
    # 请求明细仍只列真实调用记录（聚合行没有 id/时间，列出来是假的）。
    agg_logs = list(logs) + _load_rollup()

    daily_map = {}
    model_map = {}
    project_map = {}
    daily_by_model = {}
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0

    def _cache_of(record: Dict[str, Any]):
        """取一条记录的 (命中, 写入, 未命中) 输入，并夹到合法区间。"""
        prompt = max(0, int(record.get("input") or 0))
        read = max(0, min(int(record.get("cacheRead") or 0), prompt))
        write = max(0, int(record.get("cacheWrite") or 0))
        uncached = record.get("uncachedInput")
        uncached = prompt - read if uncached is None else max(0, int(uncached))
        return read, write, uncached

    def _hit_rate(cache_read: int, total_prompt: int):
        """命中率 = 命中缓存的输入 / 全部输入。

        分母用**完整输入**（含命中部分）而不是未命中部分，与官方前端
        ``cacheRead / input`` 保持一致；没有输入时返回 ``None``（而不是 0），
        让界面显示「—」而不是一个假的 0%。
        """
        if total_prompt <= 0:
            return None
        return cache_read / total_prompt

    def _bucket(store, key, extra=None):
        entry = store.get(key)
        if entry is None:
            entry = {
                "key": key,
                "total": 0,
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "uncachedInput": 0,
                "records": 0,
                "cacheHitRate": None
            }
            if extra:
                entry.update(extra)
            store[key] = entry
        return entry

    for l in agg_logs:
        d = l.get("date", "")
        m = l.get("model", "unknown")
        inp = l.get("input", 0)
        out = l.get("output", 0)
        tot = inp + out

        # 升级前的老记录、以及由云端 credit 折算出来的记录都没有缓存明细，
        # 一律按「全部未命中」计入（cacheRead = 0）—— 这是保守但诚实的下界，
        # 不假装命中、也不把这些记录从分母里抠掉。
        cr, cw, unc = _cache_of(l)

        total_input += inp
        total_output += out
        total_cache_read += cr
        total_cache_write += cw

        def _acc(store):
            entry = store
            # 按「真实调用次数」累计，而不是按行数：
            # rollup 里的一行代表当时被折算掉的 N 次调用，
            # 一律 +1 会把历史压缩后的调用量算少。
            n = _rec_count(l)
            entry["total"] += tot
            entry["input"] += inp
            entry["output"] += out
            entry["cacheRead"] += cr
            entry["cacheWrite"] += cw
            entry["uncachedInput"] += unc
            entry["records"] += n

        _bucket(daily_map, d)
        _acc(daily_map[d])

        _bucket(model_map, m, {"model": m})
        _acc(model_map[m])

        # 用量分布「按账号」：网关自己归因，官方接口不会给出非当前账号的明细
        pname = l.get("accountName") or l.get("accountId") or "未归因（升级前记录）"
        _bucket(project_map, pname)
        _acc(project_map[pname])

        # 趋势图「按模型筛选」：dailyByModel[模型] = 该模型按天的序列
        per_model = daily_by_model.setdefault(m, {})
        _bucket(per_model, d)
        _acc(per_model[d])

    # 每个桶都补上自己那一档的命中率
    for bucket in list(daily_map.values()) + list(model_map.values()) \
            + list(project_map.values()) \
            + [b for per in daily_by_model.values() for b in per.values()]:
        bucket["cacheHitRate"] = _hit_rate(bucket["cacheRead"], bucket["input"])

    daily_list = sorted(list(daily_map.values()), key=lambda x: x["key"])
    # 前端 ave 组件用 models[].key 构建「按模型筛选」下拉，必须带 key
    models_list = sorted(list(model_map.values()), key=lambda x: x["total"], reverse=True)
    for item in models_list:
        item.setdefault("key", item.get("model"))
    projects_list = sorted(list(project_map.values()), key=lambda x: x["total"], reverse=True)
    daily_by_model_list = {
        k: sorted(list(v.values()), key=lambda x: x["key"])
        for k, v in daily_by_model.items()
    }

    # 按照前端 fve 与 uve 组件的严苛结构填充每个请求项
    # 显式按时间倒序（最新在前），避免上游 / 云端流水拼接顺序影响展示方向
    #
    # 只遍历**真实明细** logs，不含 rollup 聚合行：
    # 聚合行没有 id/时间戳，列进「请求明细」会变成一堆 req-0 的假记录。
    logs_sorted = sorted(
        logs,
        key=lambda l: l.get("timestamp") or l.get("ts") or 0,
        reverse=True,
    )
    requests_list = []
    for l in logs_sorted:
        req_id = str(l.get("id", "req-0"))
        ts = l.get("timestamp") or l.get("ts") or int(time.time() * 1000)
        inp = l.get("input", 0)
        out = l.get("output", 0)
        tot = inp + out
        acc_name = l.get("accountName") or l.get("accountId") or "未归因（升级前记录）"
        cr, cw, unc = _cache_of(l)

        requests_list.append({
            "id": req_id,
            "sessionId": req_id,
            "title": f"调用 #{req_id[:8]}",
            "project": acc_name,
            "account": acc_name,
            "accountId": l.get("accountId") or "",
            "timestamp": ts,
            "time": l.get("time", ""),
            "model": l.get("model", "unknown"),
            "input": inp,
            "output": out,
            "total": tot,
            "cacheRead": cr,
            "cacheWrite": cw,
            "uncachedInput": unc,
            "thinking": 0,
            "duration": l.get("duration", 1.0)
        })

    # 只保留最近 200 条避免内存膨胀与首页渲染压力；前端默认每页 50
    requests_list = requests_list[:200]

    # 消耗最高的调用：单次 API 调用按 Token 从高到低，标题用模型名、副标题用账号 + 请求号
    # 同样只列真实明细（rollup 聚合行没有 id，列出来是假的）
    sessions_list = []
    for l in sorted(logs, key=lambda x: (x.get("total") or 0), reverse=True)[:50]:
        req_id = str(l.get("id", "req-0"))
        acc_name = l.get("accountName") or l.get("accountId") or "未归因（升级前记录）"
        cr, cw, unc = _cache_of(l)
        sessions_list.append({
            "key": req_id,
            "sessionId": req_id,
            "title": l.get("model", "unknown"),
            "project": acc_name,
            "account": acc_name,
            "input": l.get("input", 0),
            "output": l.get("output", 0),
            "cacheRead": cr,
            "cacheWrite": cw,
            "uncachedInput": unc,
            "records": 1,
        })

    total_tokens = total_input + total_output
    records_count = sum(_rec_count(l) for l in agg_logs)
    
    summary = {
        "cacheHitRate": _hit_rate(total_cache_read, total_input),
        "cacheRead": total_cache_read,
        "cacheWrite": total_cache_write,
        "input": total_input,
        "output": total_output,
        "records": records_count,
        "total": total_tokens,
        "uncachedInput": max(0, total_input - total_cache_read)
    }
    
    # --------------------------------------------------------------------------
    # 严格按版本分流聚合：
    # workbuddy    -> 国内版账号 (variant == "cn")
    # workbuddy-ai -> 国际版账号 (variant == "ai")
    # 避免双 Tab 显示 100% 重复数据导致两边完全一样
    # --------------------------------------------------------------------------
    def aggregate_for_subset(subset_logs: List[Dict[str, Any]], src_name: str) -> Dict[str, Any]:
        sub_daily = {}
        sub_model = {}
        sub_project = {}
        sub_daily_by_model = {}
        sub_inp = 0
        sub_out = 0
        sub_cr = 0
        sub_cw = 0

        for l in subset_logs:
            d = l.get("date", "")
            m = l.get("model", "unknown")
            inp = l.get("input", 0)
            out = l.get("output", 0)
            tot = inp + out
            cr, cw, unc = _cache_of(l)

            sub_inp += inp
            sub_out += out
            sub_cr += cr
            sub_cw += cw

            def _sub_acc(store):
                n = _rec_count(l)
                store["total"] += tot
                store["input"] += inp
                store["output"] += out
                store["cacheRead"] += cr
                store["cacheWrite"] += cw
                store["uncachedInput"] += unc
                store["records"] += n

            _bucket(sub_daily, d)
            _sub_acc(sub_daily[d])

            _bucket(sub_model, m, {"model": m})
            _sub_acc(sub_model[m])

            pname = l.get("accountName") or l.get("accountId") or "未归因"
            _bucket(sub_project, pname)
            _sub_acc(sub_project[pname])

            per_m = sub_daily_by_model.setdefault(m, {})
            _bucket(per_m, d)
            _sub_acc(per_m[d])

        for b in list(sub_daily.values()) + list(sub_model.values())                 + list(sub_project.values())                 + [b for per in sub_daily_by_model.values() for b in per.values()]:
            b["cacheHitRate"] = _hit_rate(b["cacheRead"], b["input"])

        d_list = sorted(list(sub_daily.values()), key=lambda x: x["key"])
        m_list = sorted(list(sub_model.values()), key=lambda x: x["total"], reverse=True)
        for item in m_list:
            item.setdefault("key", item.get("model"))
        p_list = sorted(list(sub_project.values()), key=lambda x: x["total"], reverse=True)
        dbm_list = {k: sorted(list(v.values()), key=lambda x: x["key"]) for k, v in sub_daily_by_model.items()}

        # 请求明细/消耗排行只列**真实调用记录**，排除 rollup 聚合行
        # （聚合行没有 id/时间戳，混进来会变成一堆 req-0 的假记录）
        subset_detail = [l for l in subset_logs if l.get("id")]
        if not subset_detail:
            subset_detail = []
        sub_sorted = sorted(subset_detail, key=lambda x: x.get("timestamp") or x.get("ts") or 0, reverse=True)
        req_list = []
        for l in sub_sorted[:200]:
            req_id = str(l.get("id", "req-0"))
            ts = l.get("timestamp") or l.get("ts") or int(time.time() * 1000)
            inp = l.get("input", 0)
            out = l.get("output", 0)
            cr, cw, unc = _cache_of(l)
            acc_name = l.get("accountName") or l.get("accountId") or "未归因"
            req_list.append({
                "id": req_id,
                "sessionId": req_id,
                "title": f"调用 #{req_id[:8]}",
                "project": acc_name,
                "account": acc_name,
                "accountId": l.get("accountId") or "",
                "timestamp": ts,
                "time": l.get("time", ""),
                "model": l.get("model", "unknown"),
                "input": inp,
                "output": out,
                "total": inp + out,
                "cacheRead": cr,
                "cacheWrite": cw,
                "uncachedInput": unc,
                "thinking": 0,
                "duration": l.get("duration", 1.0)
            })

        sess_list = []
        for l in sorted(subset_detail, key=lambda x: (x.get("total") or 0), reverse=True)[:50]:
            req_id = str(l.get("id", "req-0"))
            acc_name = l.get("accountName") or l.get("accountId") or "未归因"
            cr, cw, unc = _cache_of(l)
            sess_list.append({
                "key": req_id,
                "sessionId": req_id,
                "title": l.get("model", "unknown"),
                "project": acc_name,
                "account": acc_name,
                "input": l.get("input", 0),
                "output": l.get("output", 0),
                "cacheRead": cr,
                "cacheWrite": cw,
                "uncachedInput": unc,
                "records": 1,
            })

        sub_summary = {
            "cacheHitRate": _hit_rate(sub_cr, sub_inp),
            "cacheRead": sub_cr,
            "cacheWrite": sub_cw,
            "input": sub_inp,
            "output": sub_out,
            "records": sum(_rec_count(l) for l in subset_logs),
            "total": sub_inp + sub_out,
            "uncachedInput": max(0, sub_inp - sub_cr)
        }

        return {
            "coverageEndAt": None,
            "coverageStartAt": None,
            "daily": d_list,
            "dailyByModel": dbm_list,
            "filesScanned": len(subset_logs),
            "hours": [],
            "models": m_list,
            "parseErrors": 0,
            "projects": p_list,
            "requests": req_list,
            "sessions": sess_list,
            "source": src_name,
            "summary": sub_summary
        }

    # 拆分国内版(cn)与国际版(ai)
    cn_logs = [l for l in agg_logs if l.get("variant") == "cn"]
    ai_logs = [l for l in agg_logs if l.get("variant") == "ai"]
    # 若某一边为空则兜底包含全部
    src_cn = aggregate_for_subset(cn_logs if cn_logs else logs, "workbuddy")
    src_ai = aggregate_for_subset(ai_logs if ai_logs else logs, "workbuddy-ai")

    return {
        "generatedAt": int(time.time() * 1000),
        "rangeDays": None,
        "sources": [src_cn, src_ai]
    }
