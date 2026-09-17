FROM node:20-slim

# 安装必要环境与 Python 运行时
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 workbuddy-switch
RUN npm i -g workbuddy-switch@latest

# 安装 API 网关依赖 (FastAPI + Uvicorn + HTTPX)
RUN pip3 install --no-cache-dir --break-system-packages \
    fastapi \
    uvicorn \
    httpx

WORKDIR /app
COPY gateway/ /app/gateway/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV HOME=/data
ENV WB_DATA_DIR=/data/.wb-switch
ENV PORT=18080
ENV API_PORT=18081

EXPOSE 18080 18081

ENTRYPOINT ["/entrypoint.sh"]
