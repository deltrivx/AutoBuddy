#!/bin/bash
set -e

# ------------------------------------------------------------------------------
# 容器 DNS 自愈（防御宿主机继承的无效 DNS）：
# 1) 剥离带网卡作用域的条目（如 fe80::...%br0 —— 容器内无 br0 网卡，glibc 直接报错）；
# 2) 逐个实测继承的 nameserver 是否真正应答 DNS 查询（宿主机 DHCP 可能指向 macvlan
#    容器 IP，受内核 macvlan 隔离策略限制，宿主机与 bridge 容器均无法访问，只会超时）；
# 3) 继承 DNS 全部不可用时，切换到公共 DNS（223.5.5.5 / 119.29.29.29 / 8.8.8.8）兜底。
# 若容器已由模板 --dns 注入权威 DNS（resolv.conf 带 Overrides: [nameservers] 标记），
# 说明 Docker 已完成覆盖，本段自动跳过，不做任何改写。
# ------------------------------------------------------------------------------
if ! grep -q '# Overrides: \[nameservers\]' /etc/resolv.conf 2>/dev/null; then
python3 - <<'PYDNS' 2>/dev/null || true
import socket

def probe(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.8)
    try:
        s.sendto(b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03www\x05baidu\x03com\x00\x00\x01\x00\x01", (ip, 53))
        return len(s.recvfrom(512)[0]) > 0
    except Exception:
        return False
    finally:
        s.close()

path = "/etc/resolv.conf"
keep, original = [], []
try:
    with open(path) as f:
        for line in f:
            if line.startswith("nameserver"):
                parts = line.split()
                ip = parts[1] if len(parts) > 1 else ""
                original.append(ip)
                if ip and "%" not in ip and probe(ip):
                    keep.append(ip)
except Exception:
    pass

if keep and len(keep) < len(original):
    with open(path, "w") as f:
        f.write("".join("nameserver %s\n" % ip for ip in keep))
    print("[dns] pruned unreachable/scope-qualified nameservers, kept:", ", ".join(keep))
elif not keep:
    for fb in ("223.5.5.5", "119.29.29.29", "8.8.8.8"):
        if probe(fb):
            keep.append(fb)
    if keep:
        with open(path, "w") as f:
            f.write("".join("nameserver %s\n" % ip for ip in keep))
        print("[dns] inherited DNS unavailable, switched to:", ", ".join(keep))
PYDNS
fi

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

# 全局服务直连国内上游与局域网，不导出代理到环境变量，防止模型网关 18091 误走代理导致 ConnectTimeout
unset HTTP_PROXY http_proxy HTTPS_PROXY https_proxy ALL_PROXY all_proxy 2>/dev/null || true
echo "[Proxy] 精确分流 NO_PROXY: $NO_PROXY"

# 初始化数据库
python3 -c "import gateway.db as db; db.init_db()" || true

# 1. 启动底层服务（统一通过 autobuddy 包装器启动）
echo "[AutoBuddy] 正在启动底层服务..."
autobuddy serve --no-open &
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

# 4. 启动 WorkBuddy 每日成长任务服务 (内部端口: ${WB_DAILY_PORT:-18093})
#    仅跑在容器内网（loopback），由 web_proxy 转发；托管 vendor 版签到脚本，
#    账号池国内版账号只读共用凭据，定时调度在服务内实现。
if [ "${WB_DAILY_ENABLED:-1}" = "1" ]; then
  echo "[wb-daily] 正在启动每日任务服务 (端口: ${WB_DAILY_PORT:-18093})..."
  python3 /app/gateway/wb_daily.py &
  DAILY_PID=$!
fi

trap "kill -TERM $WB_PID $PROXY_PID $GW_PID ${DAILY_PID:-0} 2>/dev/null || true" SIGTERM SIGINT

wait -n
