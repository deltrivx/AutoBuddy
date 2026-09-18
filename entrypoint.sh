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

# 2b. 初始化 CLI 绑定账号（仅当 state.json 缺失）
#     helper.cjs 依赖 state.json 的 activeAccountId 决定给 CLI 用哪个账号的 token；
#     缺失时会 fallback 到 accounts[0]，行为不确定。这里显式绑定一次，使 CLI 行为
#     确定并与 WebUI 显示一致。不覆盖已存在的 state.json（用户选择/自动轮换优先）。
python3 /app/gateway/cli_bootstrap.py || true

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
