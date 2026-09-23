#!/bin/bash
set -e

echo "=== 启动 WorkBuddy Switch & OpenAI API Gateway ==="

# 只确保数据目录存在。**不要**再创建 /data/.wb-switch/rotate —— 那是 CodeBuddy CLI
# 时代的 token 轮换目录，CLI 已于 v0.3.11 彻底移除，空目录留着纯属残留。
mkdir -p /data/.wb-switch

# 浏览器安装目录（挂载卷，参考 MoviePilot 方案：镜像不内置浏览器，容器部署时自动下载并持久化）。
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/data/.wb-switch/browsers}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

auto_install_browsers() {
    if ! ls -1 "$PLAYWRIGHT_BROWSERS_PATH" 2>/dev/null | grep -q '^chromium'; then
        echo "[browsers] 首次启动未检测到持久化 Chromium，开始自动下载内核至 $PLAYWRIGHT_BROWSERS_PATH..."
        if python3 -m playwright install chromium; then
            echo "[browsers] Chromium 内核自动下载安装完成，已持久化于挂载卷！"
        else
            echo "[browsers] Chromium 自动下载失败，可在 WebUI「账号接入」页面手动重试。"
        fi
    else
        echo "[browsers] 检测到持久化 Chromium 内核已就绪：$(ls -1 "$PLAYWRIGHT_BROWSERS_PATH" | tr '\n' ' ')"
    fi
}
# 后台异步执行内核检测与下载，不阻塞 WebUI 与 API 网关的即时启动
auto_install_browsers &


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
