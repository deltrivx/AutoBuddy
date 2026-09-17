#!/bin/bash
set -e

echo "=== 启动 WorkBuddy Switch & OpenAI API Gateway ==="

mkdir -p /data/.wb-switch/rotate

# 1. 启动官方 workbuddy-switch WebUI 服务在后台
echo "[WorkBuddy-Switch] 正在启动 WebUI (端口: ${PORT:-18080})..."
workbuddy-switch --host 0.0.0.0 --port "${PORT:-18080}" --no-open &
WB_PID=$!

# 2. 启动 Python OpenAI API 网关
echo "[Gateway] 正在启动 OpenAI API 网关 (端口: ${API_PORT:-18081})..."
python3 /app/gateway/main.py &
GW_PID=$!

# 捕获退出信号优雅终止子进程
trap "kill -TERM $WB_PID $GW_PID 2>/dev/null || true" SIGTERM SIGINT

wait -n
