import os
import json
import asyncio
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
import httpx

try:
    from gateway.token_tracker import record_token_usage, get_aggregated_token_stats
except ImportError:
    from token_tracker import record_token_usage, get_aggregated_token_stats

app = FastAPI(title="WorkBuddy OpenAI Gateway", version="1.2.0")

DATA_DIR = Path(os.getenv("WB_DATA_DIR", "/data/.wb-switch"))
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

MODEL_ALIAS_MAP = {
    "hy4": "hy3",
    "hunyuan-4": "hy3",
    "hunyuan": "hy3",
    "deepseek-chat": "deepseek-v3",
    "kimi": "kimi-k3",
    "gpt-4o": "gpt-5.4",
    "gpt-4": "gpt-5.4",
    "gpt-4o-mini": "gpt-5.6-luna",
}

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
    accounts_file = DATA_DIR / "accounts.json"
    state_file = DATA_DIR / "rotate" / "state.json"
    
    if not accounts_file.exists():
        return None
    try:
        with open(accounts_file, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        if not accounts:
            return None
            
        active_id = None
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                    active_id = state.get("activeAccountId")
            except Exception:
                pass
                
        if active_id:
            for acc in accounts:
                if acc.get("id") == active_id or acc.get("uid") == active_id:
                    return acc
        return accounts[0]
    except Exception as e:
        print(f"Error loading accounts: {e}")
        return None

@app.get("/health")
def health():
    acc = get_active_account()
    catalog = get_model_catalog()
    return {
        "status": "healthy",
        "has_active_account": acc is not None,
        "active_account_variant": acc.get("variant") if acc else None,
        "models_count": len(catalog),
        "models_auto_discovered": len(catalog) - len(BASE_MODELS),
        "rotate": _rotate_state_snapshot()
    }

@app.get("/v1/models")
@app.get("/models")
async def list_models():
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

def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # 简易而高效的 Token 估算：按 3.5 字符约 1 Token
    return max(1, int(len(text) / 3.5))

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    start_time = time.time()
    acc = get_active_account()
    if not acc or not acc.get("access_token"):
        raise HTTPException(status_code=401, detail="No active WorkBuddy/CodeBuddy account available in gateway.")

    token = acc["access_token"]
    variant = acc.get("variant", "ai")
    base_url = AI_BASE_URL if variant == "ai" else CN_BASE_URL

    body = await request.json()
    requested_stream = body.get("stream", False)
    
    raw_model = body.get("model", "hy3")
    target_model = MODEL_ALIAS_MAP.get(raw_model, raw_model)
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
            output_chunks = []
            req_id = "chatcmpl-wb"
            try:
                async for chunk in res.aiter_bytes():
                    yield chunk
                    # 抓取文本估算输出 token
                    chunk_str = chunk.decode("utf-8", errors="ignore")
                    for line in chunk_str.split("\n"):
                        line = line.strip()
                        if line.startswith("data:") and line[5:].strip() != "[DONE]":
                            try:
                                j = json.loads(line[5:].strip())
                                if "id" in j:
                                    req_id = j["id"]
                                c = j.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if c:
                                    output_chunks.append(c)
                            except Exception:
                                pass
            finally:
                await res.aclose()
                await client.aclose()
                duration = time.time() - start_time
                output_tokens = estimate_tokens("".join(output_chunks))
                record_token_usage(
                    model=raw_model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_sec=duration,
                    request_id=req_id
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
        output_tokens = estimate_tokens(collected_content)
        record_token_usage(
            model=raw_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_sec=duration,
            request_id=response_id
        )

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
                "total_tokens": input_tokens + output_tokens
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


@app.on_event("startup")
async def _start_rotate_loop() -> None:
    asyncio.create_task(account_rotate_loop())


@app.get("/rotate/status")
def rotate_status():
    return _rotate_state_snapshot()


@app.post("/rotate/run")
def rotate_run():
    return rotate_once()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("API_PORT", 18091))
    uvicorn.run(app, host="0.0.0.0", port=port)
