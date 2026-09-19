"""Token 用量采集与聚合：缓存命中率为什么曾经永远是 0。

「缓存命中率」这个数字的计算公式在官方前端里（`cacheRead / input`），我们能控制的
只有**喂进去的 cacheRead**。v0.4.4 之前这里恒填 0、summary 里恒为 null，于是无论
实际命中多少，界面上都是 0 —— 不是算法错，是根本没采这个数。

这个测试锁三件事：
1. `extract_usage()` 能认上游各种写法的缓存字段；
2. `UsageScanner` 在流式透传时能跨块拼出 usage 帧（且不缓冲正文）；
3. 聚合出来的 `cacheRead` / `input` 与命中率自洽，老记录不会被伪装成「命中 0」。

只依赖标准库。
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "gateway"))

import token_tracker  # noqa: E402

_ok = 0
_fail = []


def check(label, condition, detail=""):
    global _ok
    if condition:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _fail.append(label)
        print(f"  FAIL {label} {detail}")


_tmp = tempfile.TemporaryDirectory()
token_tracker.TRACKER_FILE = Path(_tmp.name) / "token_stats_logs.json"
# 云端流水抓取会去连容器内的官方服务，测试里直接短路，避免结果依赖环境。
token_tracker.get_official_cloud_requests = lambda: []

# ---------------------------------------------------------------------------
# 1. usage 字段解析
# ---------------------------------------------------------------------------
print("[1] 从上游 usage 提取缓存字段")
u = token_tracker.extract_usage
check("没有 usage 时返回 None（而不是编一个 0）", u({"choices": []}) is None)
check("usage 不是字典时返回 None", u({"usage": "nope"}) is None)

d1 = u({"usage": {"prompt_tokens": 1000, "completion_tokens": 20,
                  "prompt_tokens_details": {"cached_tokens": 800}}})
check("prompt_tokens_details.cached_tokens",
      d1 == {"prompt": 1000, "completion": 20, "cacheRead": 800,
             "cacheWrite": 0, "uncached": 200}, f"got {d1}")

d2 = u({"usage": {"prompt_tokens": 500, "cached_tokens": 128}})
check("顶层 cached_tokens", d2["cacheRead"] == 128, f"got {d2}")

d3 = u({"usage": {"prompt_tokens": 500, "prompt_cache_hit_tokens": 300,
                  "prompt_cache_miss_tokens": 200}})
check("prompt_cache_hit_tokens", d3["cacheRead"] == 300, f"got {d3}")
check("prompt_cache_miss_tokens 就是未命中数", d3["uncached"] == 200, f"got {d3}")

d4 = u({"usage": {"prompt_tokens": 900, "cache_read_input_tokens": 640,
                  "cache_creation_input_tokens": 64}})
check("cache_read_input_tokens", d4["cacheRead"] == 640, f"got {d4}")
check("cache_creation_input_tokens 记为写缓存", d4["cacheWrite"] == 64, f"got {d4}")

d5 = u({"usage": {"prompt_tokens": 100, "completion_tokens": 5}})
check("完全没有缓存字段时命中记为 0、未命中等于全部输入",
      d5["cacheRead"] == 0 and d5["uncached"] == 100, f"got {d5}")

# ---------------------------------------------------------------------------
# 2. 流式扫描
# ---------------------------------------------------------------------------
print("\n[2] 流式透传时捞 usage（跨块）")
frame = lambda obj: ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()

scanner = token_tracker.UsageScanner()
scanner.feed(frame({"id": "chatcmpl-1", "choices": [{"delta": {"content": "你好"}}]}))
check("先抓响应 id", scanner.response_id == "chatcmpl-1", f"got {scanner.response_id!r}")
check("正文长度按字符累计", scanner.text_chars == 2, f"got {scanner.text_chars}")

# 把 usage 帧切成三段，模拟 TCP 任意分段
usage_frame = frame({"choices": [], "usage": {"prompt_tokens": 4000,
                                              "completion_tokens": 12,
                                              "prompt_cache_hit_tokens": 3584}})
scanner.feed(usage_frame[:25])
check("半截帧不产生结果", scanner.usage is None)
scanner.feed(usage_frame[25:60])
scanner.feed(usage_frame[60:] + b"data: [DONE]\n\n")
check("跨块拼接后拿到 usage", scanner.usage is not None, f"got {scanner.usage}")
check("命中数解析正确", (scanner.usage or {}).get("cacheRead") == 3584,
      f"got {scanner.usage}")

scanner.feed(b"data: {this is not json}\n\n")
scanner.feed(b": keep-alive comment\n\n")
check("脏行 / 注释行不会炸也不会污染结果", scanner.usage["prompt"] == 4000)

# 不缓冲正文：喂一段超长正文后，内部残余只有最后一行
big = token_tracker.UsageScanner()
big.feed(frame({"choices": [{"delta": {"content": "x" * 200000}}]}))
check("超长正文不会把整段内容留在内存里",
      len(big._tail) < token_tracker.UsageScanner.MAX_TAIL,
      f"tail={len(big._tail)}")

# ---------------------------------------------------------------------------
# 3. 落盘与聚合
# ---------------------------------------------------------------------------
print("\n[3] 落盘与聚合")
token_tracker.record_token_usage(model="hy3", input_tokens=1000, output_tokens=50,
                                 request_id="r-hit", cache_read=800, cache_write=0)
token_tracker.record_token_usage(model="hy3", input_tokens=1000, output_tokens=50,
                                 request_id="r-plain")
# 升级前的老记录：连 cacheRead 字段都没有
logs = json.loads(token_tracker.TRACKER_FILE.read_text(encoding="utf-8"))
logs.append({"id": "r-legacy", "time": "2026-01-01 00:00:00", "ts": 1,
             "timestamp": 1, "date": "2026-01-01", "model": "hy3",
             "input": 8000, "output": 100, "total": 8100, "duration": 1.0,
             "accountId": "", "accountName": "", "variant": "", "source": "gateway"})
token_tracker.TRACKER_FILE.write_text(json.dumps(logs), encoding="utf-8")

hit = next(l for l in logs if l["id"] == "r-hit")
check("命中记录的 uncachedInput = 输入 - 命中",
      hit["cacheRead"] == 800 and hit["uncachedInput"] == 200, f"got {hit}")
plain = next(l for l in logs if l["id"] == "r-plain")
check("没给缓存信息时命中记为 0、未命中等于全部输入",
      plain["cacheRead"] == 0 and plain["uncachedInput"] == 1000, f"got {plain}")

stats = token_tracker.get_aggregated_token_stats()
src = stats["sources"][0]
summary = src["summary"]
# 输入合计 1000 + 1000 + 8000 = 10000，命中 800
check("summary.cacheRead 汇总正确", summary["cacheRead"] == 800, f"got {summary}")
check("summary.input 是完整输入（含命中部分）", summary["input"] == 10000,
      f"got {summary['input']}")
check("summary.uncachedInput 是未命中部分", summary["uncachedInput"] == 9200,
      f"got {summary['uncachedInput']}")
check("summary.cacheHitRate = cacheRead / input",
      abs((summary["cacheHitRate"] or 0) - 0.08) < 1e-9, f"got {summary['cacheHitRate']}")

model = next(m for m in src["models"] if m.get("key") == "hy3")
check("按模型桶里也有命中数", model["cacheRead"] == 800, f"got {model}")
check("按模型桶的命中率自洽",
      abs((model["cacheHitRate"] or 0) - 800 / 10000) < 1e-9, f"got {model['cacheHitRate']}")

day = next(d for d in src["daily"] if d["cacheRead"] > 0)
check("按天桶里也有命中数", day["cacheRead"] == 800, f"got {day}")

req = next(r for r in src["requests"] if r["id"] == "r-hit")
check("请求明细带 cacheRead", req["cacheRead"] == 800 and req["uncachedInput"] == 200,
      f"got {req}")
session = next(s for s in src["sessions"] if s["key"] == "r-hit")
check("消耗排行带 cacheRead", session["cacheRead"] == 800, f"got {session}")

# ---------------------------------------------------------------------------
# 4. 边界
# ---------------------------------------------------------------------------
print("\n[4] 边界")
token_tracker.TRACKER_FILE.unlink()
empty = token_tracker.get_aggregated_token_stats()["sources"][0]["summary"]
check("没有记录时命中率是 None（界面显示「—」而不是假的 0%）",
      empty["cacheHitRate"] is None, f"got {empty}")
check("没有记录时输入为 0", empty["input"] == 0 and empty["cacheRead"] == 0,
      f"got {empty}")

# 上游给了比输入还大的命中数（脏数据），不能让命中率超过 100%
bad = token_tracker.get_aggregated_token_stats  # noqa: F841
token_tracker.record_token_usage(model="hy3", input_tokens=100, output_tokens=1,
                                 request_id="r-bad", cache_read=9999)
fresh = json.loads(token_tracker.TRACKER_FILE.read_text(encoding="utf-8"))
check("命中数被夹到不超过输入", fresh[0]["cacheRead"] == 100, f"got {fresh[0]}")
check("未命中数不为负", fresh[0]["uncachedInput"] == 0, f"got {fresh[0]}")

_tmp.cleanup()

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
