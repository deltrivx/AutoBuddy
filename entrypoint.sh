#!/bin/bash
set -e

echo "=== 启动 WorkBuddy Switch & OpenAI API Gateway ==="

mkdir -p /data/.wb-switch/rotate

# 1. 启动官方 workbuddy-switch 在 57890 端口
echo "[WorkBuddy-Switch] 正在启动底层服务..."
workbuddy-switch serve --no-open &
WB_PID=$!

# 等待 57890 端口就绪
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:57890/api/status >/dev/null 2>&1; then
        echo "[WorkBuddy-Switch] 底层服务已就绪 (57890)"
        break
    fi
    sleep 0.5
done

# 2. 自动接入 CodeBuddy CLI (配置 helper 与 state)
echo "[CodeBuddy-CLI] 自动同步 CLI 接入状态..."
curl -s -X POST http://127.0.0.1:57890/api/codebuddy-cli/install-helper >/dev/null 2>&1 || true

# 3. 启动 Python WebUI 代理 (端口: ${PORT:-18090})，注入统一图标与反代前端
echo "[WebUI-Proxy] 正在启动 WebUI 代理 (端口: ${PORT:-18090})..."
python3 /app/gateway/web_proxy.py &
PROXY_PID=$!

# 4. 启动 Python OpenAI API 网关 (端口: ${API_PORT:-18091})
echo "[Gateway] 正在启动 OpenAI API 网关 (端口: ${API_PORT:-18091})..."
python3 /app/gateway/main.py &
GW_PID=$!

trap "kill -TERM $WB_PID $PROXY_PID $GW_PID 2>/dev/null || true" SIGTERM SIGINT

wait -n
