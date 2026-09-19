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

import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

TRACKER_FILE = Path("/data/.wb-switch/token_stats_logs.json")


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
        TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        logs = []
        if TRACKER_FILE.exists():
            try:
                with open(TRACKER_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except Exception:
                logs = []
                
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
        
        logs.append(entry)
        if len(logs) > 3000:
            logs = logs[-3000:]
            
        with open(TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False)
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

    for l in logs:
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
            entry["total"] += tot
            entry["input"] += inp
            entry["output"] += out
            entry["cacheRead"] += cr
            entry["cacheWrite"] += cw
            entry["uncachedInput"] += unc
            entry["records"] += 1

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
    records_count = len(logs)
    
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
    
    def make_source_obj(src_name):
        return {
            "coverageEndAt": None,
            "coverageStartAt": None,
            "daily": daily_list,
            "dailyByModel": daily_by_model_list,
            "filesScanned": records_count,
            "hours": [],
            "models": models_list,
            "parseErrors": 0,
            "projects": projects_list,
            "requests": requests_list,
            "sessions": sessions_list,
            "source": src_name,
            "summary": summary
        }
        
    return {
        "generatedAt": int(time.time() * 1000),
        "rangeDays": None,
        # 只保留两条真实存在的产品线。官方前端原本还有 codebuddy-cli /
        # codebuddy-ide 两个来源，它们对应已移除的桌面端 CLI/IDE 功能，
        # 容器里不存在本地会话日志，属于死重，已一并去掉。
        # 前端取用逻辑（已核对）：优先当前选中项，否则回落 workbuddy，
        # 最后才用 sources[0] —— 因此删桶不会让页面空白。
        "sources": [
            make_source_obj("workbuddy"),
            make_source_obj("workbuddy-ai")
        ]
    }
