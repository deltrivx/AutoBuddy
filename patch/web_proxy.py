import os
import json
from pathlib import Path
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
import httpx
import uvicorn

app = FastAPI()

BACKEND_URL = "http://127.0.0.1:57890"
ICON_PATH = Path("/app/icon.png")

@app.get("/icon.png")
@app.get("/icon-transparent.png")
@app.get("/favicon.ico")
async def get_icon():
    if ICON_PATH.exists():
        with open(ICON_PATH, "rb") as f:
            content = f.read()
        return Response(content=content, media_type="image/png")
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BACKEND_URL}/icon.png")
        return Response(content=r.content, media_type="image/png")

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
async def proxy_all(request: Request, path: str):
    url = f"{BACKEND_URL}/{path}"
    query = str(request.query_params)
    if query:
        url = f"{url}?{query}"
    
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body
        )
        # 如果返回的是 js 静态资源或者 html，确保替换掉写死的端口
        content = r.content
        media_type = r.headers.get("content-type", "")
        if "javascript" in media_type or "text/html" in media_type:
            content = content.replace(b"http://[IP]:57890", b"")
            content = content.replace(b"http://127.0.0.1:57890", b"")
            content = content.replace(b"http://localhost:57890", b"")
            content = content.replace(b":57890", b":18090")
            content = content.replace(b"/icon-transparent.png", b"/icon.png")
            
        res_headers = {k: v for k, v in r.headers.items() if k.lower() not in ["content-encoding", "content-length", "transfer-encoding"]}
        return Response(
            content=content,
            status_code=r.status_code,
            headers=res_headers
        )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 18090))
    uvicorn.run(app, host="0.0.0.0", port=port)
