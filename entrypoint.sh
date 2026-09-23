#!/bin/bash
set -e

echo "=== 启动 AutoBuddy & OpenAI API Gateway ==="

# 只确保数据目录存在。**不要**再创建 /data/.autobuddy/rotate —— 那是 CodeBuddy CLI
# 时代的 token 轮换目录，CLI 已于 v0.3.11 彻底移除，空目录留着纯属残留。
# ==============================================================================
# 官方闭源底层服务兼容保障：
# 官方底层 wb-switch 二进制硬编码读取 $HOME/.wb-switch/accounts.json。
# 容器 HOME=/data，真实持久化数据保存在 /data/.autobuddy/。
# 建立 /data/.wb-switch 自动软链 / 同步，彻底保障官方服务读取原有账号与凭据。
# ==============================================================================
mkdir -p /data/.autobuddy
mkdir -p /data/.wb-switch

# 自动双向映射保障
for f in /data/.autobuddy/*; do
    fname=$(basename "$f")
    if [ ! -e "/data/.wb-switch/$fname" ]; then
        ln -s "/data/.autobuddy/$fname" "/data/.wb-switch/$fname" 2>/dev/null || true
    fi
done
# 反向保障（官方生成的新文件落到 .autobuddy）
for f in /data/.wb-switch/*; do
    fname=$(basename "$f")
    if [ ! -e "/data/.autobuddy/$fname" ]; then
        ln -s "/data/.wb-switch/$fname" "/data/.autobuddy/$fname" 2>/dev/null || true
    fi
done


# 浏览器安装目录（挂载卷，参考 MoviePilot 方案：镜像不内置浏览器，容器部署时自动下载并持久化）。
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/data/.autobuddy/browsers}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

# 浏览器内核由 gh_register 服务自己托管下载（见其 startup 钩子）。
# 这里只负责建目录与提示，**不再**自己拉 playwright —— 两个进程同时下载会抢
# __dirlock，结果是目录一直是空的、进度卡住不动。
if ls -1 "$PLAYWRIGHT_BROWSERS_PATH" 2>/dev/null | grep -q '^chromium'; then
    echo "[browsers] 已检测到持久化 Chromium 内核：$(ls -1 "$PLAYWRIGHT_BROWSERS_PATH" | tr '\n' ' ')"
else
    echo "[browsers] 未检测到持久化 Chromium，将由注册服务在后台自动下载（进度见 WebUI「账号接入」）"
fi

# ==============================================================================
# 代理环境与精确分流规则
# 严格遵守局域网防回环与 Python SDK/httpx 对 NO_PROXY 精确 IP 列表的硬性约束：
# 1. 内部回环 (127.0.0.1, localhost, ::1) 与常用内网主机严禁走代理，防止内部调用 404/502；
# 2. Python httpx / requests 不认 CIDR (如 192.168.31.0/24) 或通配符，必须展开为精确主机或由用户指定；
# 3. 环境变量支持 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。
# ==============================================================================
DEFAULT_NO_PROXY="localhost,127.0.0.1,::1,192.168.31.2,192.168.31.1,192.168.31.100,192.168.31.108"
if [ -n "$NO_PROXY" ]; then
    export NO_PROXY="$DEFAULT_NO_PROXY,$NO_PROXY"
    export no_proxy="$DEFAULT_NO_PROXY,$no_proxy"
else
    export NO_PROXY="$DEFAULT_NO_PROXY"
    export no_proxy="$DEFAULT_NO_PROXY"
fi

if [ -n "$HTTP_PROXY" ]; then
    export http_proxy="$HTTP_PROXY"
    echo "[Proxy] 已加载 HTTP 代理: $HTTP_PROXY"
fi
if [ -n "$HTTPS_PROXY" ]; then
    export https_proxy="$HTTPS_PROXY"
    echo "[Proxy] 已加载 HTTPS 代理: $HTTPS_PROXY"
fi
if [ -n "$ALL_PROXY" ]; then
    export all_proxy="$ALL_PROXY"
    echo "[Proxy] 已加载全局 SOCKS/ALL 代理: $ALL_PROXY"
fi
echo "[Proxy] 精确分流 NO_PROXY: $NO_PROXY"

# 初始化数据库
python3 -c "import gateway.db as db; db.init_db()" || true

# 1. 启动官方底层服务 workbuddy-switch 在 57890 端口
echo "[AutoBuddy] 正在启动底层服务..."
workbuddy-switch serve --no-open &
WB_PID=$!

# 等待 57890 端口就绪
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:57890/api/status >/dev/null 2>&1; then
        echo "[AutoBuddy] 底层服务已就绪 (57890)"
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
