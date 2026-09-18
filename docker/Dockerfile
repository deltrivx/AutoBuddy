FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# 安装 Node.js 20, Python 3, socat 及依赖
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    gnupg \
    python3 \
    python3-pip \
    socat \
    jq \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y nodejs \
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
COPY patch/ /app/patch/
COPY icon.png /app/icon.png
RUN chmod +x /entrypoint.sh

# 对 wb-switch 二进制进行补丁修复：解决 WebUI 硬编码 57890 跨域请求报错问题
RUN python3 /app/patch/patch_binary.py /usr/lib/node_modules/workbuddy-switch/bin/wb-switch-linux-x64 \
    && python3 /app/patch/patch_binary.py /usr/lib/node_modules/workbuddy-switch/node_modules/workbuddy-switch-linux-x64/bin/wb-switch-linux-x64 2>/dev/null || true

ENV HOME=/data
ENV WB_DATA_DIR=/data/.wb-switch
ENV PORT=18090
ENV API_PORT=18091

EXPOSE 18090 18091

ENTRYPOINT ["/entrypoint.sh"]
