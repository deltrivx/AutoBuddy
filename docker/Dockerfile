FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# 安装 Node.js 20, Python 3 及依赖
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

# 全局安装并封装底层引擎。
# 将底层服务二进制整合至 /usr/local/bin/autobuddy-engine，
# 并创建标准 autobuddy 启动包装器，容器内彻底消除旧包名与旧进程名。
#
# 版本必须锁定，不能用 @latest：
# patch/patch_binary.py 的锚点依赖官方前端的具体结构，上游发版会重排 minify 变量名
# 甚至改动 DOM（0.1.36 -> 0.1.40 就发生过一次），锚点一旦失配，构建会失败（见下方补丁
# 步骤），此时需要人工复核并更新锚点，不能盲跟 latest。
#
# 升级流程：
#   npm pack workbuddy-switch-linux-x64@<新版本> && tar xzf *.tgz
#   python3 patch/patch_binary.py package/bin/wb-switch-linux-x64   # 本地先验证锚点
#   改下面这行版本号 -> 构建
ARG WB_SWITCH_VERSION=0.1.40
RUN npm i -g "workbuddy-switch@${WB_SWITCH_VERSION}" \
    && cp "$(find /usr/lib/node_modules/workbuddy-switch -type f -name 'wb-switch-linux-x64' | head -n 1)" /usr/local/bin/autobuddy-engine \
    && chmod +x /usr/local/bin/autobuddy-engine \
    && printf '#!/bin/sh\nexec /usr/local/bin/autobuddy-engine "$@"\n' > /usr/local/bin/autobuddy \
    && chmod +x /usr/local/bin/autobuddy

# 安装 API 网关依赖 (FastAPI + Uvicorn + HTTPX)。
#
# v0.9.3 起「账号接入」功能已全线移除，不再需要 playwright / patchright
# 及 Chromium 系统库，镜像体积与依赖面同步收敛。
RUN pip3 install --no-cache-dir --break-system-packages \
    fastapi \
    uvicorn \
    httpx \
    requests \
    \
    && rm -rf /root/.cache/pip

WORKDIR /app
COPY gateway/ /app/gateway/
COPY entrypoint.sh /entrypoint.sh
COPY patch/ /app/patch/
COPY icon.png /app/icon.png
RUN chmod +x /entrypoint.sh

# 给 wb-switch 二进制打补丁。
#
# 目标：只保留容器里真正有意义的能力（账号自动签到 + OpenAI 兼容 API 网关），
# 把依赖宿主桌面程序的功能从 JSX 里物理移除：
#   - 账号卡片头部/底部的 WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 三个图标与「当前账号」徽章
#   - 账号页右上角桌面程序运行状态图标、导入本机账号按钮
#   - 设置页的权限检测 / 启动设置 / 自动更新 / 限额监听 / CodeBuddy CLI 自动轮换
#   - Token 统计页的数据来源 Tab 与冗余标题
# 同时把硬编码的 http://127.0.0.1:57890 换成本页 origin，避免跨域。
#
# 两条纪律（v0.3.11 教训）：
#   1. 绝不用 `|| true` / `2>/dev/null` 吞掉失败。历史上正是这样让补丁整体失效，
#      但镜像构建仍然 success，线上三个桌面图标复活而无人察觉。
#   2. patch_binary.py 在任一锚点失配时会打印明细并以非零码退出；配合 `set -e`
#      让构建直接失败，逼迫人工复核。
RUN set -eux; \
    python3 /app/patch/patch_binary.py /usr/local/bin/autobuddy-engine; \
    if [ -f /usr/lib/node_modules/workbuddy-switch/bin/wb-switch-linux-x64 ]; then \
        python3 /app/patch/patch_binary.py /usr/lib/node_modules/workbuddy-switch/bin/wb-switch-linux-x64; \
    fi

ENV HOME=/data
ENV AB_DATA_DIR=/data/.autobuddy
ENV PORT=18090
ENV API_PORT=18091

EXPOSE 18090 18091

ENTRYPOINT ["/entrypoint.sh"]
