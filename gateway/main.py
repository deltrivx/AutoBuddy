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
    acc = get_active_account()
    token = acc.get("access_token") if acc else None
    variant = acc.get("variant", "ai") if acc else "ai"
    base_url = AI_BASE_URL if variant == "ai" else CN_BASE_URL

    if token:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {token}"}
                )
                if res.status_code == 200:
                    return res.json()
        except Exception as e:
            print(f"Fetch upstream models failed: {e}")

    # Fallback catalog
    return {
        "object": "list",
        "data": [
            {"id": "hy4", "object": "model", "owned_by": "tencent-codebuddy"},
            {"id": "deepseek-chat", "object": "model", "owned_by": "tencent-codebuddy"},
            {"id": "deepseek-reasoner", "object": "model", "owned_by": "tencent-codebuddy"},
            {"id": "claude-3-7-sonnet", "object": "model", "owned_by": "tencent-codebuddy"},
            {"id": "claude-3-5-sonnet", "object": "model", "owned_by": "tencent-codebuddy"},
            {"id": "gpt-4o", "object": "model", "owned_by": "tencent-codebuddy"}
        ]
    }

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
    stream = body.get("stream", False)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    client = httpx.AsyncClient(timeout=180.0)

    if stream:
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
        try:
            res = await client.post(f"{base_url}/chat/completions", json=body, headers=headers)
            return Response(
                content=res.content,
                status_code=res.status_code,
                headers={"Content-Type": res.headers.get("content-type", "application/json")}
            )
        finally:
            await client.aclose()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=18081)
