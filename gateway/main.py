import os
import json
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
import httpx

app = FastAPI(title="WorkBuddy OpenAI Gateway", version="1.0.0")

DATA_DIR = Path(os.getenv("WB_DATA_DIR", "/data/.wb-switch"))
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://www.codebuddy.ai/v2")
CN_BASE_URL = os.getenv("CN_BASE_URL", "https://copilot.tencent.com/v2")

MODEL_ALIAS_MAP = {
    "hy4": "hy3",
    "hunyuan-4": "hy3",
    "hunyuan": "hy3",
    "deepseek-chat": "deepseek-v3",
    "kimi": "kimi-k3",
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
        "active_account_variant": acc.get("variant") if acc else None
    }

@app.get("/v1/models")
@app.get("/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "hy4", "object": "model", "owned_by": "tencent-codebuddy"},
            {"id": "hy3", "object": "model", "owned_by": "tencent-codebuddy"},
            {"id": "deepseek-v3", "object": "model", "owned_by": "tencent-codebuddy"},
            {"id": "deepseek-chat", "object": "model", "owned_by": "tencent-codebuddy"},
            {"id": "kimi-k3", "object": "model", "owned_by": "tencent-codebuddy"}
        ]
    }

def normalize_messages_for_upstream(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not messages:
        return [{"role": "system", "content": "You are a helpful assistant."}]
    first = messages[0]
    if first.get("role") != "system":
        return [{"role": "system", "content": "You are a helpful assistant."}] + messages
    return messages

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    acc = get_active_account()
    if not acc or not acc.get("access_token"):
        raise HTTPException(status_code=401, detail="No active WorkBuddy/CodeBuddy account available in gateway.")

    token = acc["access_token"]
    variant = acc.get("variant", "ai")
    base_url = AI_BASE_URL if variant == "ai" else CN_BASE_URL

    body = await request.json()
    requested_stream = body.get("stream", False)
    
    # 别名映射
    raw_model = body.get("model", "hy4")
    target_model = MODEL_ALIAS_MAP.get(raw_model, raw_model)
    body["model"] = target_model

    if "messages" in body and isinstance(body["messages"], list):
        body["messages"] = normalize_messages_for_upstream(body["messages"])

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
            try:
                async for chunk in res.aiter_bytes():
                    yield chunk
            finally:
                await res.aclose()
                await client.aclose()

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

        result_payload = {
            "id": response_id,
            "object": "chat.completion",
            "created": 1789682000,
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
                "prompt_tokens": 10,
                "completion_tokens": len(collected_content),
                "total_tokens": 10 + len(collected_content)
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
