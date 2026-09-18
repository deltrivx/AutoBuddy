import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

TRACKER_FILE = Path("/data/.wb-switch/token_stats_logs.json")

def record_token_usage(model: str, input_tokens: int, output_tokens: int, duration_sec: float = 0.0, request_id: str = ""):
    """网关实时调用记录"""
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
        
        entry = {
            "id": request_id or f"wb-req-{ts}",
            "time": time_str,
            "ts": ts,
            "timestamp": ts,
            "date": date_str,
            "model": model,
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
            "duration": round(duration_sec, 2),
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
                "source": "official"
            })
            existing_ids.add(req_id)

    daily_map = {}
    model_map = {}
    total_input = 0
    total_output = 0
    
    for l in logs:
        d = l.get("date", "")
        m = l.get("model", "unknown")
        inp = l.get("input", 0)
        out = l.get("output", 0)
        tot = inp + out
        
        total_input += inp
        total_output += out
        
        if d not in daily_map:
            daily_map[d] = {
                "key": d,
                "total": 0,
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "uncachedInput": 0,
                "records": 0,
                "cacheHitRate": None
            }
        daily_map[d]["total"] += tot
        daily_map[d]["input"] += inp
        daily_map[d]["output"] += out
        daily_map[d]["uncachedInput"] += inp
        daily_map[d]["records"] += 1
        
        if m not in model_map:
            model_map[m] = {
                "model": m,
                "total": 0,
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "records": 0
            }
        model_map[m]["total"] += tot
        model_map[m]["input"] += inp
        model_map[m]["output"] += out
        model_map[m]["records"] += 1

    daily_list = sorted(list(daily_map.values()), key=lambda x: x["key"])
    models_list = sorted(list(model_map.values()), key=lambda x: x["total"], reverse=True)
    
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

        requests_list.append({
            "id": req_id,
            "sessionId": req_id,
            "title": f"调用 #{req_id[:8]}",
            "project": "WorkBuddy Gateway",
            "timestamp": ts,
            "time": l.get("time", ""),
            "model": l.get("model", "unknown"),
            "input": inp,
            "output": out,
            "total": tot,
            "cacheRead": 0,
            "cacheWrite": 0,
            "uncachedInput": inp,
            "thinking": 0,
            "duration": l.get("duration", 1.0)
        })

    # 只保留最近 200 条避免内存膨胀与首页渲染压力；前端默认每页 50
    requests_list = requests_list[:200]

    total_tokens = total_input + total_output
    records_count = len(logs)
    
    summary = {
        "cacheHitRate": None,
        "cacheRead": 0,
        "cacheWrite": 0,
        "input": total_input,
        "output": total_output,
        "records": records_count,
        "total": total_tokens,
        "uncachedInput": total_input
    }
    
    def make_source_obj(src_name):
        return {
            "coverageEndAt": None,
            "coverageStartAt": None,
            "daily": daily_list,
            "dailyByModel": {},
            "filesScanned": records_count,
            "hours": [],
            "models": models_list,
            "parseErrors": 0,
            "projects": [],
            "requests": requests_list,
            "sessions": [],
            "source": src_name,
            "summary": summary
        }
        
    return {
        "generatedAt": int(time.time() * 1000),
        "rangeDays": None,
        "sources": [
            make_source_obj("workbuddy"),
            make_source_obj("workbuddy-ai"),
            make_source_obj("codebuddy-cli"),
            make_source_obj("codebuddy-ide")
        ]
    }
