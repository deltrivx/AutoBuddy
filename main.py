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

# 实测通过的全部官方模型清单
SUPPORTED_MODELS = [
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
    return {
        "status": "healthy",
        "has_active_account": acc is not None,
        "active_account_variant": acc.get("variant") if acc else None,
        "models_count": len(SUPPORTED_MODELS)
    }

@app.get("/v1/models")
@app.get("/models")
async def list_models():
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
            for m in SUPPORTED_MODELS
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("API_PORT", 18091))
    uvicorn.run(app, host="0.0.0.0", port=port)
