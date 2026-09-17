#!/bin/bash
set -e

echo "=== 启动 WorkBuddy Switch & OpenAI API Gateway ==="

mkdir -p /data/.wb-switch/rotate

# 1. 启动官方 workbuddy-switch WebUI 服务在后台 (serve 命令, 绑定 0.0.0.0, 默认 57890 转发)
echo "[WorkBuddy-Switch] 正在启动 WebUI (端口: ${PORT:-18090})..."
socat TCP-LISTEN:${PORT:-18090},fork,reuseaddr TCP:127.0.0.1:57890 &
SOCAT_PID=$!

workbuddy-switch serve --no-open &
WB_PID=$!

# 2. 启动 Python OpenAI API 网关
echo "[Gateway] 正在启动 OpenAI API 网关 (端口: ${API_PORT:-18091})..."
python3 /app/gateway/main.py &
GW_PID=$!

trap "kill -TERM $WB_PID $GW_PID $SOCAT_PID 2>/dev/null || true" SIGTERM SIGINT

wait -n
