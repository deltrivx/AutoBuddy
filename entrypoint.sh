#!/bin/bash
set -e

echo "=== 启动 WorkBuddy Switch & OpenAI API Gateway ==="

# 只确保数据目录存在。**不要**再创建 /data/.wb-switch/rotate —— 那是 CodeBuddy CLI
# 时代的 token 轮换目录，CLI 已于 v0.3.11 彻底移除，空目录留着纯属残留。
mkdir -p /data/.wb-switch

# 浏览器安装目录（挂载卷）。镜像不再内置 Chromium，这里只建空目录；
# 首次使用需在 UI 里点「下载浏览器」，下完后容器重建也不丢。
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/data/.wb-switch/browsers}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
if ls -1 "$PLAYWRIGHT_BROWSERS_PATH" 2>/dev/null | grep -q '^chromium'; then
    echo "[browsers] 已检测到内置浏览器：$(ls -1 "$PLAYWRIGHT_BROWSERS_PATH" | tr '\n' ' ')"
else
    echo "[browsers] 尚未下载浏览器，请到 WebUI 的「账号接入」面板点「下载浏览器」"
fi

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

# 2. 启动 Python WebUI 代理 (端口: ${PORT:-18090})，注入统一图标与反代前端
echo "[WebUI-Proxy] 正在启动 WebUI 代理 (端口: ${PORT:-18090})..."
python3 /app/gateway/web_proxy.py &
PROXY_PID=$!

# 3. 启动 Python OpenAI API 网关 (端口: ${API_PORT:-18091})
echo "[Gateway] 正在启动 OpenAI API 网关 (端口: ${API_PORT:-18091})..."
python3 /app/gateway/main.py &
GW_PID=$!

# 4. 启动 GitHub 自动化注册服务 (内部端口: ${GH_REGISTER_PORT:-18092})
#    仅跑在容器内网（loopback），由 web_proxy 的三条路由转发，不对外映射端口。
if [ "${GH_REGISTER_ENABLED:-1}" = "1" ]; then
  echo "[gh-register] 正在启动 GitHub 注册服务 (端口: ${GH_REGISTER_PORT:-18092})..."
  python3 /app/gateway/gh_register.py &
  GH_PID=$!
fi

trap "kill -TERM $WB_PID $PROXY_PID $GW_PID ${GH_PID:-0} 2>/dev/null || true" SIGTERM SIGINT

wait -n
